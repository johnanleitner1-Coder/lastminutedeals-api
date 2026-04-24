# Last Minute Deals HQ

[![Smithery](https://smithery.ai/badge/@johnanleitner1/Last_Minute_Deals_HQ)](https://smithery.ai/server/johnanleitner1/Last_Minute_Deals_HQ)
[![lastminutedeals-api MCP server](https://glama.ai/mcp/servers/johnanleitner1-Coder/lastminutedeals-api/badges/score.svg)](https://glama.ai/mcp/servers/johnanleitner1-Coder/lastminutedeals-api)

MCP server with real-time last-minute tour and activity inventory. Live bookable slots across 32 suppliers in 47 countries and 100+ cities, sourced live from production booking systems via the [OCTO open standard](https://docs.octo.travel/). Inventory refreshed every 4 hours.

Search available slots and create Stripe checkout sessions — customers pay on our page, suppliers are confirmed automatically.

## Install

### Claude Desktop

```bash
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client claude
```

### Claude Code

```bash
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client claude-code
```

### Cursor

```bash
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client cursor
```

### Windsurf

```bash
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client windsurf
```

### Other MCP Clients

```bash
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client <client-name>
```

Or connect directly to the remote MCP endpoint:

```
https://api.lastminutedealshq.com/mcp
```

## Tools

| Tool | Description |
|---|---|
| `search_slots` | Search available tours and activities. Filter by city, category, `hours_ahead` window, and `max_price`. Returns live inventory sorted by urgency (soonest first). |
| `book_slot` | Book a slot for a customer. **Approval mode** (default) returns a Stripe checkout URL for the customer to pay. **Autonomous mode** charges a pre-funded wallet and returns a confirmation number directly. Supports quantity for group bookings. |
| `preview_slot` | Get a shareable booking page URL for a slot. The user clicks the link, sees full details, enters their own name/email/phone, and pays via Stripe. Use this when a human is browsing with an AI assistant. |
| `get_booking_status` | Check booking status by `booking_id`. Returns status, confirmation number, checkout URL (recoverable if lost), service details, and payment status. |
| `get_supplier_info` | Returns the full supplier network — destinations, experience types, booking platform, and confirmation speed. Use before searching to understand what inventory is available. |

## Example

**"What tours are available in Rome this weekend under $50?"**

```
search_slots(city="Rome", hours_ahead=72, max_price=50)
```

```json
[
  {
    "service_name": "E-Bike Tour of Ancient Rome & Appian Way",
    "business_name": "Bicycle Roma",
    "start_time": "2026-04-19T09:00:00+00:00",
    "price": 42.00,
    "currency": "EUR",
    "location_city": "Rome",
    "hours_until_start": 26.5,
    "slot_id": "a1b2c3..."
  },
  {
    "service_name": "Castelli Romani Wine & Food E-Bike Tour",
    "business_name": "Bicycle Roma",
    "start_time": "2026-04-19T13:00:00+00:00",
    "price": 48.50,
    "currency": "EUR",
    "location_city": "Rome",
    "hours_until_start": 30.5,
    "slot_id": "d4e5f6..."
  }
]
```

**"Book the e-bike tour for Jane Smith"**

```
book_slot(
  slot_id="a1b2c3...",
  customer_name="Jane Smith",
  customer_email="jane@example.com",
  customer_phone="+15550001234"
)
```

```json
{
  "booking_id": "bk_a1b2c3_x9y8z7",
  "status": "pending_payment",
  "checkout_url": "https://checkout.stripe.com/c/pay/...",
  "message": "Customer should complete payment at checkout_url"
}
```

**"Did she pay yet?"**

```
get_booking_status(booking_id="bk_a1b2c3_x9y8z7")
```

```json
{
  "status": "booked",
  "confirmation_number": "BR-20260419-001",
  "service_name": "E-Bike Tour of Ancient Rome & Appian Way",
  "start_time": "2026-04-19T09:00:00+00:00",
  "payment_status": "captured"
}
```

## Suppliers

23 active suppliers. Live inventory across Iceland, Italy, Mexico, Morocco, Portugal, Japan, Tanzania, Finland, Montenegro, Romania, Egypt, Turkey, United States, United Kingdom, and China.

| Supplier | Destinations | Experiences |
|---|---|---|
| Arctic Adventures | Reykjavik, Husafell, Skaftafell, Iceland | Glacier hikes, ice caves, snowmobiling, aurora tours, whale watching, diving |
| Bicycle Roma | Rome, Appia Antica, Castelli Romani | E-bike tours, food tours, guided city tours, bike rentals |
| Boka Bliss | Kotor, Montenegro | Boat tours, sea caves, coastal experiences |
| EgyExcursions | Cairo, Egypt | Pyramids, cultural tours, day trips |
| Hillborn Experiences | Arusha, Serengeti, Zanzibar, Kilimanjaro | Private safaris, Kilimanjaro climbs, ultra-luxury wildlife tours |
| Íshestar Riding Tours | Selfoss, Iceland | Horse riding, glacier rides, Viking tours |
| Marvel Egypt Tours | Cairo, Luxor, Aswan | Pyramids, Nile cruises, temple tours |
| O Turista Tours | Lisbon, Porto, Sintra, Fatima, Nazaré | Private tours, day trips, wine experiences |
| Pure Morocco Experience | Marrakech, Sahara Desert | Desert tours, multi-day tours, cultural experiences |
| Ramen Factory Kyoto | Kyoto, Japan | Cooking classes, ramen workshops |
| REDRIB Experience | Helsinki, Finland | Speed boat tours, archipelago experiences |
| TourTransfer Bucharest | Bucharest, Romania | City tours, Dracula castle, Peles castle |
| Tours El Chiquiz | Puerto Vallarta, Mexico | Tequila tasting, hiking, nightlife tours, botanical gardens |
| Trivanzo Holidays | Cairo, Luxor, Red Sea, Egypt | Nile cruises, cultural tours, desert tours |
| TUTU VIEW Ltd | Shanghai, Xi'an, Beijing, Chengdu, Hangzhou | Multi-day tours, Silk Road, food tours, nature tours |
| Vakare Travel Service | Antalya, Turkey | Boat tours, jeep safaris, cultural excursions |
| All Washington View | Washington D.C. | City tours, sightseeing, monuments, panoramic views |
| Zestro Bizlinks | Japan | Experiences |
| Adi Tours - Nuba travel | Cairo, Egypt | Pyramids, cultural tours, Nile excursions, desert tours, day trips |
| The Photo Experience | London, United Kingdom | Photography tours, photo walks, city photography experiences |
| Sailing Windermere | Windermere, Lake District, United Kingdom | Sailing, lake cruises |
| Perfect Day Tours | Luxor, Egypt | Temple tours, Valley of the Kings |
| Nefertiti Tours | Cairo, Giza, Egypt | Pyramids, cultural tours |
| Blue Dolphin Sailing | Guanacaste, Costa Rica | Sailing tours, sunset cruises, snorkeling |
| EGYPT GATE | Cairo, Egypt | Tours and experiences |
| Imperio tours | Rome, Italy | Fiat 500 tours, golf cart tours, food tours |
| VIDABOA | Porto, Douro Valley, Portugal | Wine tours, private tours |
| Gallo Tour | Rome, Italy | Golf cart tours |
| Food Activity Japan | Osaka, Japan | Matcha making, food experiences |

## Categories

`experiences` · `wellness` · `beauty` · `hospitality`

## API Key

Free. No credit card required. Needed for booking operations — search works without one.

```bash
curl -X POST https://api.lastminutedealshq.com/api/keys/register \
  -H "Content-Type: application/json" \
  -d '{"name": "MyAgent", "email": "agent@example.com"}'
```

```json
{"api_key": "lmd_..."}
```

Pass the key when configuring the MCP server or as `X-API-Key` header for REST calls.

## REST API

Base URL: `https://api.lastminutedealshq.com`

| Endpoint | Method | Description |
|---|---|---|
| `/api/slots` | GET | Search slots — `city`, `category`, `hours_ahead`, `max_price` |
| `/api/book` | POST | Create Stripe checkout for a slot |
| `/api/book/direct` | POST | Book with pre-funded wallet (autonomous agents) |
| `/bookings/{id}` | GET | Check booking status |
| `/api/keys/register` | POST | Get a free API key |
| `/api/wallets/create` | POST | Create a pre-funded agent wallet |
| `/api/wallets/fund` | POST | Get Stripe link to fund wallet |
| `/health` | GET | System health check |
| `/metrics` | GET | Live system metrics |

## Booking Modes

**Approval (default)** — Returns a Stripe checkout URL. The customer visits the link, pays, and the booking is confirmed with the supplier automatically. Best for human-in-the-loop flows.

**Autonomous** — Requires a pre-funded wallet. The system debits the wallet instantly and confirms with the supplier. No redirect, no latency. Best for fully autonomous agents.

```
book_slot(
  slot_id="...",
  customer_name="Jane Smith",
  customer_email="jane@example.com",
  mode="autonomous",
  wallet_id="wlt_..."
)
```

## How It Works

```
Every 4 hours:
  fetch_octo_slots.py   →  Pull availability from 32 suppliers via OCTO API
  aggregate_slots.py    →  Deduplicate, filter, sort by urgency
  compute_pricing.py    →  Dynamic commission-based pricing
  sync_to_supabase.py   →  Upsert to production database

MCP/REST requests:
  Agent calls search_slots  →  Supabase query  →  Live results
  Agent calls book_slot     →  Stripe checkout  →  OCTO booking  →  Supplier confirmed
```

## Status

- **Slots live:** 5,000+
- **Suppliers:** 17
- **Countries:** 15
- **Refresh interval:** Every 4 hours
- **Uptime:** Hosted on Railway (24/7)
- **Payments:** Stripe (authorization-then-capture — customer is never charged for a failed booking)
