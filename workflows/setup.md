# Workflow: One-Time Setup

## Objective
Set up all infrastructure needed to run the last-minute booking slot pipeline.
Run this workflow exactly once. Never re-run it (each step creates new resources).

**Current strategy**: We are an **execution infrastructure API for AI agents** built on
the OCTO standard. Agents call `search_slots()` + `book_slot()` and get real confirmation
numbers back. Supply comes from OCTO-compliant platforms (Ventrata, Bokun, Peek, Rezdy)
— no scraping, no Playwright for the core booking path.

## Prerequisites
- Python 3.11+ installed
- Git installed (for landing page deployment)
- Google Cloud Console project created (for Sheets only)

---

## Step 1 — Install Python dependencies

```bash
cd "c:/Users/janaa/Agentic Workflows"
pip install -r tools/requirements.txt
playwright install chromium
```

Playwright is only needed by `complete_booking.py` for Mindbody fallback.
OCTO + Rezdy bookings use pure HTTP — no Playwright required.

---

## Step 2 — Get OCTO supplier API keys (do this first — it's the core supply)

Priority order — start from the top:

### 1. Ventrata (highest priority — test sandbox available immediately)
- Go to docs.ventrata.com → get a sandbox API key (no signup needed for testing)
- For production: email connectivity@ventrata.com
- Add to `.env`: `VENTRATA_API_KEY=<key>`
- Test: `python tools/test_octo_connection.py --platform ventrata`

### 2. Rezdy (reseller API — gated, requires email approval)
- The "Start free trial" signup at rezdy.com is for **Operators** (tour businesses) only — not useful for us
- Email **partnerintegrations@rezdy.com** to request reseller API access
- Subject: "Reseller API Access Request" — briefly describe that you're building a booking aggregation API
- They will respond with an onboarding form or next steps
- Add to `.env`: `REZDY_API_KEY=<key>` once received
- Test: `python tools/test_octo_connection.py --platform rezdy`

### 3. Bokun ($49/month self-serve — 1.5% booking fee)
- Sign up at bokun.io → START plan
- Get API key from Settings → API Credentials
- Add to `.env`: `BOKUN_API_KEY=<key>`
- Test: `python tools/test_octo_connection.py --platform bokun`

### 4. Peek Pro
- Email ben.smithart@peek.com — introduce as an OCTO reseller
- Add to `.env`: `PEEK_API_KEY=<key>` when received

### 5. FareHarbor
- Apply at fareharbor.com/partners
- Add to `.env`: `FAREHARBOR_API_KEY=<key>` when approved

After adding any key, always run:
```bash
python tools/test_octo_connection.py
```
This verifies auth, lists products, and checks availability on one product.

---

## Step 3 — Verify first OCTO slot fetch

Once you have at least one API key in `.env`:

```bash
# Fetch OCTO slots (Ventrata, Bokun, etc.)
python tools/fetch_octo_slots.py --hours-ahead 72

# Fetch Rezdy slots (if REZDY_API_KEY is set)
python tools/fetch_rezdy_slots.py --hours-ahead 72

# Aggregate and deduplicate
python tools/aggregate_slots.py

# Compute pricing
python tools/compute_pricing.py
```

Check `.tmp/aggregated_slots.json` — confirm records appear with `our_price` populated.

---

## Step 4 — Google Cloud setup (for Sheets logging)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g., "LastMinuteDeals")
3. Enable: **Google Sheets API** and **Google Drive API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: Desktop app
   - Download as `credentials.json`
   - Move to: `c:/Users/janaa/Agentic Workflows/credentials.json`

---

## Step 5 — Create the Google Sheet

```bash
python tools/setup_google_sheets.py --title "Last Minute Deals" --email YOUR_EMAIL@gmail.com
```

This opens a browser for OAuth consent. After authorizing:
- It creates `token.json` (do not delete)
- Prints a `GOOGLE_SHEET_ID`

Copy the Sheet ID into `.env`:
```
GOOGLE_SHEET_ID=<paste here>
```

Then write slots to Sheets:
```bash
python tools/write_to_sheets.py
```

Open your Google Sheet — confirm "Slots" tab has rows, "RunLog" has one entry.

---

## Step 6 — Fill in remaining .env API keys

```
# ── Core OCTO supply (see Step 2) ──────────────────────────────────────
VENTRATA_API_KEY=
REZDY_API_KEY=
BOKUN_API_KEY=
XOLA_API_KEY=
PEEK_API_KEY=
FAREHARBOR_API_KEY=

# ── Google Sheets (see Steps 4–5) ──────────────────────────────────────
GOOGLE_SHEET_ID=

# ── Stripe (enables "Book Now" checkout) ───────────────────────────────
STRIPE_SECRET_KEY=           # sk_test_... for testing, sk_live_... for production
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
BOOKING_API_URL=http://localhost:5050/api/book   # update after deploying

# ── Affiliate IDs (apply now — approval takes days to weeks) ───────────
BOOKING_COM_AFFILIATE_ID=    # partner.booking.com
EXPEDIA_AFFILIATE_ID=        # expediapartnersolutions.com
TRIPADVISOR_AFFILIATE_ID=    # tripadvisor.com/affiliates
BOOKSY_AFFILIATE_CODE=       # booksy.com/partners

# ── Social distribution ─────────────────────────────────────────────────
TWITTER_BEARER_TOKEN=
TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_SECRET=

REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=LastMinuteDealsBot/1.0

TELEGRAM_BOT_TOKEN=          # From @BotFather on Telegram
TELEGRAM_CHANNEL_ID=         # @yourchannel or numeric ID

# ── Mindbody agency account (for Mindbody booking fulfillment only) ─────
MINDBODY_AGENCY_EMAIL=
MINDBODY_AGENCY_PASSWORD=
MINDBODY_AGENCY_CARD_NUMBER=
MINDBODY_AGENCY_CARD_EXP=
MINDBODY_AGENCY_CARD_CVV=

# ── SMS alerts (optional, Phase 4+) ────────────────────────────────────
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=

# ── Other social (optional, Phase 5+) ──────────────────────────────────
INSTAGRAM_ACCESS_TOKEN=
FACEBOOK_PAGE_ACCESS_TOKEN=
TIKTOK_ACCESS_TOKEN=
LINKEDIN_ACCESS_TOKEN=
```

---

## Step 7 — Set up Stripe (enables "Book Now" checkout)

1. Create account at [dashboard.stripe.com](https://dashboard.stripe.com)
2. Go to **Developers → API Keys**
3. Copy **Secret key** (starts with `sk_test_` for testing, `sk_live_` for production)
4. Add to `.env`: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
5. Start the booking server:
   ```bash
   python tools/run_api_server.py
   ```
6. Set up webhook:
   - Stripe Dashboard → Developers → Webhooks → Add endpoint
   - URL: `https://YOUR_SERVER/api/webhook`
   - Events: `checkout.session.completed`
   - Copy signing secret → add as `STRIPE_WEBHOOK_SECRET`

**Testing**: Use card `4242 4242 4242 4242`, any future date, any CVV.

**Deploy the booking server** (needed for real traffic):
- **Railway** (recommended): `railway up` from this folder
- **Render**: Web Service, start command `python tools/run_api_server.py`
- **Fly.io**: `fly launch && fly deploy`
- After deploy, update `BOOKING_API_URL` in `.env` and rebuild landing page

---

## Step 8 — MCP Server (connect to Claude Desktop / Claude Code)

The MCP server exposes our slot inventory as AI-callable tools.

### Start in HTTP mode (for testing):
```bash
python tools/run_mcp_server.py --http --port 5051
```

Test endpoints:
```
GET  http://localhost:5051/health
GET  http://localhost:5051/slots?city=NYC&category=experiences&hours_ahead=24
POST http://localhost:5051/book
GET  http://localhost:5051/bookings/<booking_id>
```

### Connect to Claude Desktop (stdio mode):

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "lastminutedeals": {
      "command": "C:/Users/janaa/AppData/Local/Programs/Python/Python313/python.exe",
      "args":    ["c:/Users/janaa/Agentic Workflows/tools/run_mcp_server.py"],
      "cwd":     "c:/Users/janaa/Agentic Workflows"
    }
  }
}
```

**Note**: Do NOT add `--stdio` — FastMCP handles stdio mode automatically.
After adding, restart Claude Desktop completely.

### Claude Code (already configured):
`~/.claude/settings.json` has been updated. Claude Code can call:
- `search_last_minute_slots` — find slots by city / category / time
- `get_slot` — full details for a specific slot_id
- `book_slot` — execute booking (returns confirmation number)
- `get_booking_status` — check booking by booking_id
- `refresh_slots` — trigger fresh fetch from OCTO suppliers

### Test from Claude:
> "Find me last-minute tour or activity deals in New York under $100"
> "Book slot [slot_id] for John Smith, john@example.com, 555-0100"

---

## Step 9 — Set up social accounts (Phase 4)

**Twitter/X**
- Create account at twitter.com
- Apply for developer access at developer.twitter.com
- Create a Project + App, get Bearer Token + API keys
- Add to `.env`

**Reddit**
- Create account at reddit.com
- Build up karma before posting (lurk + upvote for a week)
- Register app at reddit.com/prefs/apps → "script" type
- Add to `.env`

**Telegram**
- Message @BotFather on Telegram: `/newbot`
- Create a public channel (e.g., `@lastminutedeals`)
- Add bot as admin of the channel
- Add token and channel ID to `.env`

---

## Step 10 — Apply for affiliate programs (can run in parallel)

Apply now — approval takes days to weeks:
- **Booking.com**: partner.booking.com → Affiliate Partner Program
- **Expedia**: expediapartnersolutions.com
- **TripAdvisor**: tripadvisor.com/affiliates
- **Booksy**: booksy.com/business → Partner program

Add IDs to `.env` as they're approved. System works without them (no affiliate links until IDs are set).

---

## Step 11 — Automate with Windows Task Scheduler

Run the pipeline every 4 hours automatically:

1. Open **Task Scheduler** (search in Start menu)
2. Click **Create Basic Task**
3. Name: `LastMinuteDeals Pipeline`
4. Trigger: **Daily**, repeat every **4 hours**
5. Action: **Start a program**
   - Program: `C:\Users\janaa\Agentic Workflows\run_pipeline.bat`
   - Start in: `C:\Users\janaa\Agentic Workflows`
6. Click Finish

To run immediately: right-click the task → **Run**

---

## Step 12 — SeatGeek API (event inventory)

1. Go to [platform.seatgeek.com](https://platform.seatgeek.com)
2. Create a developer account → get `client_id` and `client_secret`
3. Add to `.env`:
   ```
   SEATGEEK_CLIENT_ID=...
   SEATGEEK_CLIENT_SECRET=...
   ```

---

## Step 13 — Twilio SMS alerts (optional, Phase 4+)

1. Sign up at [twilio.com](https://twilio.com) (free trial gives ~$15 credit)
2. Get a phone number in the Console
3. Add to `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
4. SMS alerts run automatically at the end of each pipeline cycle
5. Subscribers opt in via the landing page SMS form

Test (dry run — no messages sent):
```bash
python tools/send_sms_alert.py --dry-run
```

---

## Notes
- `credentials.json` and `token.json` are gitignored — never commit them
- `.env` is gitignored — never commit it
- If `token.json` expires, delete it and re-run `setup_google_sheets.py` to re-authenticate
- Booking confirmation artifacts (JSON) are saved to `.tmp/booking_confirmations/` for debugging
- The `tools/seeds/` directory holds supplier configs — `octo_suppliers.json` controls which OCTO platforms are active
- Channel directory for city routing: `tools/channel_directory.json` — add new cities as needed
- Deal visuals for Instagram/TikTok: `python tools/generate_deal_visual.py --top-n 5`
