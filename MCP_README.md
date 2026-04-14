# Last Minute Deals — MCP Server

**Real-time last-minute booking execution for AI agents.**

Search available tour, activity, and experience slots and book them directly — all from Claude or any MCP-compatible agent. No screen-scraping, no Playwright automation. Pure API, instant confirmation.

---

## What it does

| Tool | What it does |
|---|---|
| `search_slots` | Find available slots by city, category, time window, price |
| `get_slot` | Full details for a specific slot |
| `book_slot` | Create a booking (Stripe checkout or wallet) |
| `get_booking_status` | Check confirmation status by booking ID |
| `refresh_slots` | Trigger a fresh inventory pull from connected suppliers |

### Example conversation

```
User: Find me a last-minute bike tour in Rome today

Claude: [calls search_slots(city="Rome", category="experiences", hours_ahead=24)]
→ Rome E-Bike City Highlights Tour — starts in 6h — $54 — Instant confirmation

User: Book it for Jane Smith, jane@example.com

Claude: [calls book_slot(slot_id="...", customer_name="Jane Smith", ...)]
→ Booking created. Checkout link sent to Jane.
```

---

## Remote HTTP Endpoint

No local installation required. The MCP server is hosted and accessible directly over HTTP — compatible with any agent or HTTP client that speaks JSON-RPC 2.0.

**Endpoint:** `POST https://api.lastminutedealshq.com/mcp`

All requests use `Content-Type: application/json` and the standard MCP JSON-RPC 2.0 envelope.

### Initialize session

```bash
curl -X POST https://api.lastminutedealshq.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "clientInfo": { "name": "my-agent", "version": "1.0" },
      "capabilities": {}
    }
  }'
```

### List available tools

```bash
curl -X POST https://api.lastminutedealshq.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'
```

### Call a tool — search_slots

```bash
curl -X POST https://api.lastminutedealshq.com/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "search_slots",
      "arguments": {
        "city": "Rome",
        "category": "experiences",
        "hours_ahead": 24,
        "max_price": 100
      }
    }
  }'
```

**Supported tools via `tools/call`:** `search_slots`, `book_slot`, `get_booking_status`, `get_supplier_info`

> This endpoint works with any HTTP-capable agent — no stdio transport, no local Python process, no package installation needed. Pass your API key in the `Authorization: Bearer` header.

---

## Installation

### Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "lastminutedeals": {
      "command": "python",
      "args": ["-m", "lmd_mcp"],
      "env": {
        "LMD_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

> **Alternative:** If you prefer not to run a local process, you can point any HTTP-capable MCP client directly at `https://api.lastminutedealshq.com/mcp` instead of using the stdio transport above. See the [Remote HTTP Endpoint](#remote-http-endpoint) section for details.

### Claude Code / any MCP client

```bash
pip install lmd-mcp
```

```json
{
  "mcpServers": {
    "lastminutedeals": {
      "command": "lmd-mcp",
      "env": { "LMD_API_KEY": "your-api-key-here" }
    }
  }
}
```

Get a free API key at **[lastminutedealshq.com/developers](https://lastminutedealshq.com/developers)**

---

## Tool reference

### `search_slots`

Search for available last-minute booking slots.

```python
search_slots(
    city="Rome",            # optional — city name, partial match
    category="experiences", # optional — experiences | wellness | beauty | events
    hours_ahead=48,         # max hours until start (default: 48)
    max_price=100,          # max price in USD (optional)
    limit=20                # max results (default: 20)
)
```

Returns a list of slot objects sorted by `hours_until_start` ascending.

**Slot object fields:**
```json
{
  "slot_id": "abc123",
  "service_name": "Rome E-Bike City Highlights Tour",
  "business_name": "Bicycle Roma",
  "category": "experiences",
  "location_city": "Rome",
  "location_country": "IT",
  "start_time": "2026-04-13T14:00:00Z",
  "hours_until_start": 6.0,
  "price": 49.0,
  "our_price": 54.0,
  "currency": "USD",
  "spots_open": 4
}
```

---

### `book_slot`

Book a slot for a customer. Initiates a Stripe Checkout session.

```python
book_slot(
    slot_id="abc123",
    customer_name="Jane Smith",
    customer_email="jane@example.com",
    customer_phone="+15550001234"
)
```

Returns:
```json
{
  "booking_id": "bk_a3f9c12e",
  "checkout_url": "https://checkout.stripe.com/...",
  "status": "awaiting_payment"
}
```

After payment, the booking is automatically confirmed on the supplier's system and the customer receives a confirmation email.

**Agent wallets (no checkout redirect):** Pre-fund a wallet and bookings complete without a customer-facing checkout page. Useful for fully autonomous agent workflows. See [Wallet API docs](https://lastminutedealshq.com/developers#wallets).

---

### `get_booking_status`

```python
get_booking_status(booking_id="bk_a3f9c12e")
```

Returns current status: `awaiting_payment` | `confirmed` | `failed` | `cancelled`

---

### `refresh_slots`

```python
refresh_slots(hours_ahead=48)
```

Triggers a fresh pull from all connected suppliers. Slot data is also refreshed automatically every 4 hours.

---

## Supplier network

Inventory is sourced through direct API integrations — no scraping:

| Platform | Protocol | Coverage |
|---|---|---|
| Ventrata | OCTO | Big Bus Tours, City Sightseeing, Fat Tire Tours, 80+ operators |
| Bokun | OCTO | Bicycle Roma, Arctic Adventures, 50k+ global suppliers |
| Zaui | OCTO | Canadian outdoor activities |
| Peek Pro | OCTO | US tours and activities |

All suppliers have **instant confirmation** — no manual approval, no phone calls.

---

## REST API (non-MCP clients)

The same tools are available as a REST API for agents that don't use MCP:

```
GET  https://api.lastminutedealshq.com/slots?city=Rome&hours_ahead=24
GET  https://api.lastminutedealshq.com/slots/{slot_id}
POST https://api.lastminutedealshq.com/book
GET  https://api.lastminutedealshq.com/bookings/{booking_id}
```

Full OpenAPI spec at [api.lastminutedealshq.com/docs](https://api.lastminutedealshq.com/docs)

---

## Pricing

| Tier | Cost | Limit |
|---|---|---|
| Free | $0 | 100 searches/day, 10 bookings/month |
| Developer | $29/mo | Unlimited searches, 500 bookings/month |
| Production | $99/mo | Unlimited everything + webhook support |

No per-booking fees. Get started at **[lastminutedealshq.com/developers](https://lastminutedealshq.com/developers)**

---

## Links

- **Website:** [lastminutedealshq.com](https://lastminutedealshq.com)
- **Developer docs:** [lastminutedealshq.com/developers](https://lastminutedealshq.com/developers)
- **API reference:** [api.lastminutedealshq.com/docs](https://api.lastminutedealshq.com/docs)
- **Contact:** bookings@lastminutedealshq.com
