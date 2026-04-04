# Last Minute Deals HQ — Agent API

**Base URL:** `https://web-production-dc74b.up.railway.app`
**Landing page:** `https://lastminutedealshq.com`

You are talking to a last-minute booking API. It has live inventory of available slots across wellness, experiences, events, and beauty — all starting within 72 hours. You can search inventory, get a price quote, and execute a booking end-to-end. Payment is handled for you.

---

## Authentication

All write endpoints require an API key in the header:

```
X-API-Key: lmd_your_key_here
```

Get a free key in one call:

```http
POST /api/keys/register
Content-Type: application/json

{ "name": "MyAgent", "email": "agent@example.com" }
```

Response:
```json
{ "success": true, "api_key": "lmd_..." }
```

Use this key on every subsequent request. It never expires.

---

## Core Booking Flow

There are two flows depending on your use case.

### Flow A — Redirect (human in the loop)

Best when a human will complete payment in a browser.

```
1. POST /api/book          → returns Stripe checkout URL
2. Human pays at URL       → Stripe fires webhook
3. Webhook executes booking on source platform automatically
```

### Flow B — Autonomous (agent-native, no redirect)

Best for fully automated agents. Requires a pre-funded wallet or a saved Stripe customer.

```
1. POST /api/wallets/create  → create wallet
2. POST /api/wallets/fund    → top up via Stripe (one-time human step)
3. POST /execute/guaranteed  → booking executed + confirmed in one call
```

---

## Endpoints

### Search inventory

```http
GET /slots
```

Parameters (all optional):

| Param | Type | Default | Description |
|---|---|---|---|
| `category` | string | — | `wellness`, `experiences`, `events`, `beauty`, `professional_services` |
| `city` | string | — | Partial match, case-insensitive |
| `hours_ahead` | int | 72 | Only return slots starting within this many hours |
| `max_price` | float | — | Filter by `our_price` ≤ this value |
| `limit` | int | 50 | Max results (cap: 500) |

Returns slots sorted by `hours_until_start` ascending — soonest first.

```http
GET /slots?category=wellness&city=New+York&hours_ahead=24&max_price=150
```

**Key fields in each slot:**

```json
{
  "slot_id":          "abc123",
  "service_name":     "60-min Deep Tissue Massage",
  "category":         "wellness",
  "location_city":    "New York",
  "location_state":   "NY",
  "start_time":       "2026-04-04T14:00:00Z",
  "hours_until_start": 6.3,
  "our_price":        120.00,
  "currency":         "USD",
  "platform":         "octo"
}
```

Note: `booking_url` is never returned. It is internal only.

---

### Get a price quote

Confirms availability and locks in the price before committing.

```http
GET /slots/{slot_id}/quote
```

```json
{
  "slot_id":     "abc123",
  "service_name": "60-min Deep Tissue Massage",
  "our_price":   120.00,
  "currency":    "USD",
  "available":   true,
  "start_time":  "2026-04-04T14:00:00Z"
}
```

---

### Book — redirect flow (human pays)

```http
POST /api/book
X-API-Key: lmd_your_key_here
Content-Type: application/json

{
  "slot_id":        "abc123",
  "customer_name":  "Jane Smith",
  "customer_email": "jane@example.com",
  "customer_phone": "+15550001234"
}
```

Response:
```json
{
  "success":      true,
  "checkout_url": "https://checkout.stripe.com/c/pay/..."
}
```

Direct the human to `checkout_url`. Once they pay, the booking is executed automatically via Stripe webhook. The card is held (not charged) until the booking is confirmed on the source platform. If booking fails, the hold is cancelled and the customer is never charged.

---

### Book — autonomous flow (agent pays from wallet)

No redirect. Returns only when the outcome is known.

```http
POST /execute/guaranteed
X-API-Key: lmd_your_key_here
Content-Type: application/json

{
  "slot_id":   "abc123",
  "category":  "wellness",
  "city":      "New York",
  "hours_ahead": 24,
  "budget":    150.0,
  "allow_alternatives": true,
  "customer": {
    "name":  "Jane Smith",
    "email": "jane@example.com",
    "phone": "+15550001234"
  },
  "wallet_id": "wlt_..."
}
```

`slot_id` is optional — if omitted, the engine selects the best available slot matching your constraints. Set `allow_alternatives: true` to permit this.

Response:
```json
{
  "success":           true,
  "status":            "booked",
  "confirmation":      "EVT-12345",
  "booking_id":        "bk_abc123",
  "slot_id":           "abc123",
  "service_name":      "60-min Deep Tissue Massage",
  "price_charged":     120.00,
  "savings_vs_market": 15.00,
  "attempts":          1,
  "fallbacks_used":    0
}
```

---

### Goal-oriented booking (let the engine decide)

Tell the API what you want to achieve. It picks the slot.

```http
POST /execute/best
X-API-Key: lmd_your_key_here
Content-Type: application/json

{
  "goal":        "maximize_value",
  "city":        "New York",
  "category":    "wellness",
  "budget":      150.0,
  "hours_ahead": 48,
  "customer":    { "name": "...", "email": "...", "phone": "..." },
  "wallet_id":   "wlt_...",
  "explain":     true
}
```

Goals:

| Value | Behaviour |
|---|---|
| `maximize_value` | Best discount vs market rate |
| `minimize_wait` | Soonest available slot |
| `maximize_success` | Highest platform reliability × confidence score |
| `minimize_price` | Cheapest absolute price within budget |

Set `explain: true` to get a reasoning field in the response.

---

### Persistent intent (fire-and-forget)

Create a standing order. The system monitors inventory and executes when a match appears — you don't need to poll.

```http
POST /intent/create
X-API-Key: lmd_your_key_here
Content-Type: application/json

{
  "goal": "find_and_book",
  "constraints": {
    "category":    "wellness",
    "city":        "New York",
    "budget":      150.0,
    "hours_ahead": 48,
    "allow_alternatives": true
  },
  "customer":    { "name": "...", "email": "...", "phone": "..." },
  "payment":     { "method": "wallet", "wallet_id": "wlt_..." },
  "autonomy":    "full",
  "callback_url": "https://your-agent.example.com/webhook",
  "ttl_hours":   24
}
```

`autonomy` options:

| Value | Behaviour |
|---|---|
| `full` | Auto-executes when a match is found |
| `notify` | POSTs to `callback_url`, waits for confirmation before booking |
| `monitor` | Never executes — alerts only |

```json
{ "intent_id": "int_...", "status": "monitoring", "expires_at": "..." }
```

Check status: `GET /intent/{intent_id}`
Cancel: `POST /intent/{intent_id}/cancel`

---

### Cancel a booking

```http
DELETE /bookings/{booking_id}
X-API-Key: lmd_your_key_here
```

`booking_id` is the `bk_...` value returned when the booking was created.

Flow: cancels on the source platform first (with automatic retry up to 3× with backoff), then issues a full Stripe refund. If the platform cancel fails transiently, it queues for automatic retry every 15 minutes for up to 12 hours. The customer refund always goes through regardless.

```json
{
  "success":               true,
  "booking_id":            "bk_abc123",
  "status":                "cancelled",
  "platform_result":       "Cancelled on ventrata_edinexplore (HTTP 200)",
  "stripe_result":         "refunded",
  "refund_id":             "re_...",
  "octo_queued_for_retry": false,
  "cancelled_at":          "2026-04-04T00:25:41Z"
}
```

---

## Agent Wallets

Wallets let agents pay without a Stripe redirect. Fund once, book many times.

### Create a wallet

```http
POST /api/wallets/create
X-API-Key: lmd_your_key_here
Content-Type: application/json

{ "name": "MyAgent", "email": "agent@example.com" }
```

```json
{ "wallet_id": "wlt_...", "api_key": "lmd_...", "balance": 0.00 }
```

### Fund a wallet

```http
POST /api/wallets/fund
X-API-Key: lmd_your_key_here
Content-Type: application/json

{ "wallet_id": "wlt_...", "amount_dollars": 200 }
```

Returns a Stripe Checkout URL. Complete it once to load the balance. After that, all bookings debit the wallet automatically — no human in the loop.

### Check balance

```http
GET /api/wallets/{wallet_id}/balance
X-API-Key: lmd_your_key_here
```

---

## Webhooks (push notifications)

Subscribe to receive a POST to your endpoint when matching deals appear — no polling needed.

```http
POST /api/webhooks/subscribe
X-API-Key: lmd_your_key_here
Content-Type: application/json

{
  "callback_url": "https://your-agent.example.com/deals",
  "filters": {
    "category":    "wellness",
    "city":        "New York",
    "max_price":   150.0,
    "hours_ahead": 24
  }
}
```

Unsubscribe: `POST /api/webhooks/unsubscribe` with `{ "subscription_id": "..." }`.

---

## Market Insights

Read-only intelligence endpoints. No API key required.

```http
GET /insights/market?category=wellness&city=New+York
GET /insights/platform/{platform_name}
```

Returns price distributions, fill rates, average discounts, and platform reliability scores. Useful for deciding when and where to book.

---

## MCP Server

This API is MCP-compatible. Send JSON-RPC 2.0 requests to `POST /mcp`.

Available tools:
- `search_slots` — maps to `GET /slots`
- `get_quote` — maps to `GET /slots/{id}/quote`
- `book_slot` — maps to `POST /api/book`
- `execute_guaranteed` — maps to `POST /execute/guaranteed`
- `cancel_booking` — maps to `DELETE /bookings/{id}`
- `get_market_insights` — maps to `GET /insights/market`

Example:
```json
{
  "jsonrpc": "2.0",
  "method":  "search_slots",
  "params":  { "category": "wellness", "city": "New York", "hours_ahead": 24 },
  "id":      1
}
```

---

## Complete example — autonomous booking in 3 calls

```python
import requests

BASE = "https://web-production-dc74b.up.railway.app"

# 1. Get API key (once)
key = requests.post(f"{BASE}/api/keys/register",
    json={"name": "MyAgent", "email": "me@example.com"}).json()["api_key"]
H = {"X-API-Key": key, "Content-Type": "application/json"}

# 2. Search for a wellness slot in New York under $150
slots = requests.get(f"{BASE}/slots",
    params={"category": "wellness", "city": "New York",
            "hours_ahead": 24, "max_price": 150},
    headers=H).json()
slot_id = slots[0]["slot_id"]

# 3. Book it (redirect flow — human pays)
checkout = requests.post(f"{BASE}/api/book",
    json={"slot_id": slot_id, "customer_name": "Jane Smith",
          "customer_email": "jane@example.com",
          "customer_phone": "+15550001234"},
    headers=H).json()

print(checkout["checkout_url"])  # send to human, booking completes automatically
```

---

## Health check

```http
GET /health
```

```json
{
  "status": "ok",
  "slots":  11209,
  "uptime": "..."
}
```

---

## Rate limits

- `GET /slots`: 120 requests/minute
- Write endpoints: 30 requests/minute
- No hard limits on free tier currently — subject to change

---

## Support

Issues and API access: open an issue at the GitHub repo or email the team at `api@lastminutedealshq.com`.
