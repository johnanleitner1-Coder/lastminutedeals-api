# LastMinuteDeals — Execution Infrastructure for AI Agents

**The intent-to-confirmation layer for real-world service bookings.**

An AI agent tells LastMinuteDeals what it wants to book. The system searches, decides, executes, retries on failure, and returns a confirmed outcome — synchronously or via persistent delegated intent.

```python
from lmd_sdk import LastMinuteDeals

lmd = LastMinuteDeals.register("MyAgent", "agent@example.com")  # get your API key

# Fire-and-forget delegated intent
intent = lmd.intent(
    goal="find_and_book",
    category="wellness",
    city="New York",
    budget=120,
    customer={"name": "Jane Smith", "email": "jane@example.com", "phone": "+15550001234"},
    wallet_id="wlt_...",           # pre-funded wallet — no per-booking redirect
    callback_url="https://your-agent.com/webhook",
)
# System monitors continuously, executes when a match appears, POSTs to callback_url
```

---

## Why this exists

When an AI agent needs to book a real-world service, it has two options:

| Build in-house | Use LastMinuteDeals |
|---|---|
| 3–6 months of engineering | 1 API key, 1 HTTP call |
| Platform-specific Playwright automation | 8+ platforms already built |
| No historical data | Market insights accumulate from day 1 |
| Breaks when platforms change | Maintained continuously |
| No retry logic | 7-strategy fallback engine |
| No wallet system | Pre-funded wallets, instant execution |

---

## Core capabilities

### Guaranteed Execution
`POST /execute/guaranteed` — synchronous, hard outcome. Returns only when booking is confirmed or all paths exhausted.

7 fallback strategies in order:
1. Original slot
2. Retry original (transient failure)
3. Similar slot (same category/city, ±2h)
4. Any slot (same category/city)
5. Any platform (same category/city)
6. Metro area match
7. Alternative category (if allowed)

### Goal-Oriented Decisioning
`POST /execute/best` — tell us what you want, we decide what to book.

Goals: `maximize_value` · `minimize_wait` · `maximize_success` · `minimize_price`

### Delegated Intent Sessions
`POST /intent/create` — create a persistent goal. System works until done.

Autonomy levels:
- `full` — auto-execute when matching slots appear (fire and forget)
- `notify` — POST to callback_url first, wait for your approval
- `monitor` — observe and alert only, never execute

Goal types:
- `find_and_book` — monitor + execute
- `monitor_only` — notify when slots appear
- `price_alert` — notify when price drops below target

### Agent Wallets
Pre-funded accounts. Deposit once, execute many times — no Stripe redirect per booking.

```python
wallet = lmd.wallet_create("MyAgent", "agent@example.com")
fund_url = lmd.wallet_fund(wallet["wallet_id"], amount_dollars=100)
# Customer visits fund_url once. Wallet now has $100.
# Future bookings debit instantly — no redirect, no latency.
```

### Market Intelligence
`GET /insights/market` — data that compounds over time and is not reproducible from a standing start.

- Platform success rates (which platforms complete bookings reliably)
- Fill velocity per category (how fast slots sell out)
- Optimal booking windows (when to book for best success rate)
- Competing demand signals (how many other agents are hunting the same category/city)

### Real-Time Watchers
Continuous slot watchers update inventory every 30–60 seconds per platform (vs. 4-hour polling cycle). New slots trigger instant downstream pricing, Supabase sync, and webhook notifications.

---

## Quick start

### 1. Get an API key (free)
```bash
curl -X POST https://api.lastminutedealshq.com/api/keys/register \
  -H "Content-Type: application/json" \
  -d '{"name": "MyAgent", "email": "agent@example.com"}'
# → {"api_key": "lmd_..."}
```

### 2. Search slots
```bash
curl "https://api.lastminutedealshq.com/slots?city=NYC&category=wellness&hours_ahead=24"
```

### 3. Guaranteed booking (wallet)
```bash
# First, create and fund a wallet:
curl -X POST .../api/wallets/create -d '{"name":"MyAgent","email":"agent@example.com"}'
curl -X POST .../api/wallets/fund -d '{"wallet_id":"wlt_...","amount_dollars":100}'
# Visit checkout_url once to fund. Then:

curl -X POST .../execute/guaranteed \
  -H "X-API-Key: lmd_..." \
  -d '{
    "category": "wellness", "city": "New York", "hours_ahead": 24,
    "customer": {"name":"Jane","email":"jane@example.com","phone":"+15550001234"},
    "wallet_id": "wlt_..."
  }'
# → {"success":true,"status":"booked","confirmation":"MB-12345","attempts":1,"confidence_score":0.82,...}
```

### 4. Python SDK (zero dependencies)
```bash
# No install needed — drop the file in your project:
curl -O https://lastminutedealshq.com/lmd_sdk.py
```

```python
from lmd_sdk import LastMinuteDeals

lmd = LastMinuteDeals(api_key="lmd_...")
result = lmd.execute(category="wellness", city="NYC", customer={...}, wallet_id="wlt_...")
```

---

## API reference

Full OpenAPI 3.1 spec: `https://lastminutedealshq.com/openapi.json`

| Endpoint | Description |
|---|---|
| `GET /slots` | Search available slots |
| `GET /slots/{id}/quote` | Confirm availability + price |
| `POST /api/book` | Stripe checkout (user redirect) |
| `POST /execute/guaranteed` | Synchronous guaranteed outcome |
| `POST /execute/best` | Goal-optimized selection + execution |
| `POST /intent/create` | Persistent delegated intent |
| `GET /intent/{id}` | Check intent status |
| `POST /intent/{id}/execute` | Manually trigger notify-autonomy intent |
| `GET /intent/list` | All your active intents |
| `POST /api/wallets/create` | Create pre-funded wallet |
| `POST /api/wallets/fund` | Stripe link to fund wallet |
| `GET /api/wallets/{id}/balance` | Current balance |
| `GET /insights/market` | Market intelligence snapshot |
| `GET /insights/platform/{name}` | Per-platform performance data |
| `GET /metrics` | Public system performance metrics |
| `POST /mcp` | MCP-over-HTTP (no transport needed) |
| `POST /api/webhooks/subscribe` | Subscribe to deal alert webhooks |
| `GET /api/watcher/status` | Real-time data freshness |

Authentication: `X-API-Key: lmd_...` header (free registration at `/api/keys/register`)

---

## MCP server

For Claude, GPT, and any MCP-compatible agent:

```
POST /mcp
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_slots","arguments":{"city":"NYC","category":"experiences"}}}
```

Available tools: `search_slots` · `book_slot` · `get_booking_status` · `get_supplier_info`

Remote endpoint: `https://web-production-dc74b.up.railway.app/mcp`

---

## Example agents

| File | Pattern |
|---|---|
| `examples/wellness_booking_agent.py` | Direct booking + intent fallback |
| `examples/event_finder_agent.py` | minimize_wait goal + monitoring intent |
| `examples/concierge_agent.py` | Multi-category natural-language concierge |

---

## Response format

Every execution response includes `system_context` — live proof that the infrastructure behind the booking is healthy:

```json
{
  "success": true,
  "status": "booked",
  "confirmation": "EVT-98765",
  "price_charged": 65.00,
  "attempts": 2,
  "fallbacks_used": 1,
  "confidence_score": 0.79,
  "system_context": {
    "system_success_rate": 0.91,
    "live_bookable_slots": 8432,
    "data_freshness_seconds": 23,
    "total_bookings_processed": 1847
  }
}
```

---

## Platforms covered

| Platform | Category | Method |
|---|---|---|
| Eventbrite | Entertainment | Public API |
| Mindbody | Wellness, Fitness | Open API |
| Luma | Events | Public API |
| Meetup | Events | GraphQL |
| SeatGeek | Entertainment | Public API |
| Ticketmaster | Entertainment | Discovery API |
| Dice.fm | Entertainment | Web API |
| Booksy | Beauty, Wellness | Partner API |
| FareHarbor | Activities | Partner API |

---

## Self-hosting

```bash
git clone https://github.com/YOUR_USERNAME/lastminutedeals
cd lastminutedeals
pip install -r tools/requirements.txt
cp .env.example .env   # fill in your keys
python tools/run_api_server.py
```

Minimum viable `.env`:
```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
BOOKING_SERVER_HOST=https://your-api-domain.com
LANDING_PAGE_URL=https://lastminutedealshq.com
```
