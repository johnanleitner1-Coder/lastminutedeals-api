# OCTO Integration Workflow

Runbook for activating OCTO-compliant booking platforms and Rezdy as supply sources
for the LastMinuteDeals pipe.

**The goal:** A single API call from an AI agent triggers a real booking at a real supplier
and returns a confirmation number. No human in the loop.

---

## Architecture

```
Supplier APIs (Ventrata / Bokun / Rezdy / Peek / Xola)
    ↓
fetch_octo_slots.py + fetch_rezdy_slots.py
    ↓
aggregate_slots.py  →  aggregated_slots.json
    ↓
run_mcp_server.py   →  MCP tools: search_slots / book_slot
    ↓
complete_booking.py →  OCTOBooker / RezdyBooker (pure HTTP)
    ↓
Supplier confirmation number returned to agent
```

---

## Step 1 — Ventrata (Start Here)

**Why first:** Test sandbox available immediately, no signup, 1-2 day production approval.

### 1a. Get test API key
1. Go to: `https://docs.ventrata.com/getting-started/getting-started`
2. Copy the test API key from the page (it's publicly documented for the EdinExplore test supplier)
3. Add to `.env`: `VENTRATA_API_KEY=<key>`

### 1b. Enable Ventrata in supplier config
Edit `tools/seeds/octo_suppliers.json`:
```json
{
  "supplier_id": "ventrata_edinexplore",
  "enabled": true,
  ...
}
```

### 1c. Test the fetch
```bash
python tools/fetch_octo_slots.py --test-only
```
Expected output: slots from EdinExplore (fictional Edinburgh attraction, includes various tour types).

### 1d. Test the MCP server search
```bash
python tools/run_mcp_server.py --http
curl "http://localhost:5051/slots?category=experiences&hours_ahead=72&limit=5"
```

### 1e. Test booking (sandbox)
```bash
curl -X POST http://localhost:5051/book \
  -H "Content-Type: application/json" \
  -d '{"slot_id":"<slot_id_from_search>","customer_name":"Test User","customer_email":"test@example.com","customer_phone":"+15550001234"}'
```
Expected: `{"success": true, "confirmation": "OCTO-<uuid>", ...}`

### 1f. Apply for production access
Email: `connectivity@ventrata.com`
Subject: "OCTO Reseller Integration — LastMinuteDeals"
Body: Describe what you're building (AI agent booking execution pipe), attach evidence of your sandbox integration working.
Approval: 1-2 business days. They'll email you a production API key.

---

## Step 2 — Rezdy (Free, 48h Approval)

**Why second:** Free account, faster inventory access than OCTO since many suppliers are already on Rezdy.

### 2a. Create Rezdy account
1. Go to: `rezdy.com`
2. Sign up, select **"Reseller"** during signup (not Operator)
3. Go to **Integrations** menu → Request API Key
4. Wait ~48 hours for key to be provisioned

### 2b. Add API key to .env
```
REZDY_API_KEY=<your_key>
```

### 2c. Test in staging first
```bash
python tools/fetch_rezdy_slots.py --staging
```
Note: A new account has limited supplier access. The "Rezdy Agent Certification" test supplier is available to all accounts for testing purposes.

### 2d. Request supplier access
In the Rezdy Marketplace, browse suppliers and send rate access requests. Each supplier individually approves agents. As approvals come in, inventory grows automatically on next run.

### 2e. Run production fetch
```bash
python tools/fetch_rezdy_slots.py
```

---

## Step 3 — Bokun ($49/month, Self-Serve)

### 3a. Sign up
1. Go to: `bokun.io`
2. Select **Reseller** role during signup
3. Choose START plan ($49/month — 14-day free trial available)

### 3b. Generate API key
Settings → Connections → API Keys → Generate

### 3c. Get demo environment
Email `support@bokun.io`: "I'm building a reseller integration and need demo environment credentials."

### 3d. Add to .env and enable
```
BOKUN_API_KEY=<your_key>
```
In `tools/seeds/octo_suppliers.json`, set `enabled: true` for `bokun_reseller`.

Note: Bokun charges **1.5% per booking** via API. Factor this into pricing.

---

## Step 4 — Peek Pro (Email for Access)

Email: `ben.smithart@peek.com`
Subject: "Peek Pro Reseller API Access Request"
Body: Brief description of LastMinuteDeals, what you're building, expected use case.

Once approved, set `PEEK_API_KEY` in `.env` and enable `peek_pro` in `octo_suppliers.json`.

---

## Running the Full Pipeline

### Manual run (all platforms)
```bash
python tools/fetch_octo_slots.py --hours-ahead 168
python tools/fetch_rezdy_slots.py --hours-ahead 168
python tools/aggregate_slots.py --hours-ahead 168
python tools/compute_pricing.py
```

### Automated (Windows Task Scheduler)
`run_pipeline.bat` includes OCTO and Rezdy fetch steps. Schedule every 4 hours via `schedule_pipeline.xml`.

### Via MCP (from Claude Desktop or agent)
```
search_slots(category="experiences", hours_ahead=48, city="New York")
→ returns list of available slots

book_slot(slot_id="...", customer_name="Jane Smith", customer_email="jane@example.com", customer_phone="+15550001234")
→ calls OCTOBooker or RezdyBooker
→ returns confirmation number
```

---

## Defining Success

A booking test passes when **all five** of these are true:

| Criterion | How to verify |
|---|---|
| Booking completed | `book_slot` returns `success: true` |
| Confirmation returned | Response has `confirmation` field with real supplier reference |
| Payment processed | Credits consumed or card charged on supplier account |
| Booking visible in supplier system | Log into Ventrata/Bokun/Rezdy dashboard and find the booking |
| Customer name correctly attached | Booking shows customer's name, not agency name |

A partial result (e.g. confirmation returned but not visible in dashboard) is diagnostic data, not a pass.

---

## Failure Taxonomy

If `book_slot` fails, categorize the failure before debugging:

| Error type | Likely cause | Fix |
|---|---|---|
| `BookingAuthRequired` | API key not set or wrong | Check `.env`, verify `api_key_env` matches key name |
| `BookingUnavailableError` (409) | Slot sold out between fetch and book | Normal race condition — retry with different slot |
| `BookingUnavailableError` (422) | Malformed booking params | Check `product_id`, `option_id`, `availability_id` in booking_url JSON |
| `BookingTimeoutError` | Network timeout | Retry; if persistent, check supplier API status |
| `BookingUnknownError` | Unexpected response | Read full error message; check supplier API docs |
| No slots returned by search | API key not set, supplier has no availability, or time window too narrow | Check key, widen `hours_ahead`, check supplier dashboard |

---

## Key Files

| File | Purpose |
|---|---|
| `tools/seeds/octo_suppliers.json` | Supplier registry — enable/disable each platform here |
| `tools/fetch_octo_slots.py` | OCTO availability fetcher (Ventrata, Bokun, Peek, Xola, Zaui) |
| `tools/fetch_rezdy_slots.py` | Rezdy Agent API fetcher (own format, free account) |
| `tools/complete_booking.py` | OCTOBooker + RezdyBooker — pure HTTP booking execution |
| `tools/run_mcp_server.py` | MCP server — exposes search_slots / book_slot to AI agents |
| `.env` | API keys — VENTRATA_API_KEY, REZDY_API_KEY, BOKUN_API_KEY, etc. |
| `.env.example` | Template with instructions for every key |

---

*Last updated: 2026-03-30*
