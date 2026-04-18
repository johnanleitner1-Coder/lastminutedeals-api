# Workflow: One-Time Setup

## Objective
Set up all infrastructure needed to run the last-minute booking slot pipeline.
Run this workflow exactly once. Never re-run it (each step creates new resources).

**Current strategy**: We are an **execution infrastructure API for AI agents** built on
the OCTO standard. Agents call `search_slots()` + `book_slot()` and get real confirmation
numbers back. Supply comes from OCTO-compliant platforms (Ventrata, Bokun, Peek)
— no scraping, no Playwright for the core booking path.

## Prerequisites
- Python 3.11+ installed

---

## Step 1 — Install Python dependencies

```bash
cd "c:/Users/janaa/Agentic Workflows"
pip install -r tools/requirements.txt
```

---

## Step 2 — Get OCTO supplier API keys (do this first — it's the core supply)

Priority order — start from the top:

### 1. Bokun ($49/month self-serve — 1.5% booking fee)
- Sign up at bokun.io -> START plan
- Get API key from Settings -> API Credentials
- Add to `.env`: `BOKUN_API_KEY=<key>`
- Test: `python tools/test_octo_connection.py --platform bokun`

### 2. Ventrata (test sandbox available immediately)
- Go to docs.ventrata.com -> get a sandbox API key
- For production: email connectivity@ventrata.com
- Add to `.env`: `VENTRATA_API_KEY=<key>`
- Test: `python tools/test_octo_connection.py --platform ventrata`

### 3. Peek Pro
- Email ben.smithart@peek.com — introduce as an OCTO reseller
- Add to `.env`: `PEEK_API_KEY=<key>` when received

After adding any key, always run:
```bash
python tools/test_octo_connection.py
```

---

## Step 3 — Verify first slot fetch

Once you have at least one API key in `.env`:

```bash
python tools/fetch_octo_slots.py --hours-ahead 168
python tools/aggregate_slots.py --hours-ahead 168
python tools/compute_pricing.py
```

Check `.tmp/aggregated_slots.json` — confirm records appear with `our_price` populated.

---

## Step 4 — Fill in remaining .env API keys

```
# ── Core OCTO supply (see Step 2) ──────────────────────────────────────
BOKUN_API_KEY=
VENTRATA_API_KEY=
PEEK_API_KEY=
XOLA_API_KEY=

# ── Supabase (database) ───────────────────────────────────────────────
SUPABASE_URL=
SUPABASE_SECRET_KEY=

# ── Stripe (enables "Book Now" checkout) ───────────────────────────────
STRIPE_SECRET_KEY=
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
BOOKING_API_URL=http://localhost:5050

# ── SendGrid (booking confirmation emails) ────────────────────────────
SENDGRID_API_KEY=
```

---

## Step 5 — Set up Stripe (enables "Book Now" checkout)

1. Create account at [dashboard.stripe.com](https://dashboard.stripe.com)
2. Go to **Developers -> API Keys**
3. Copy **Secret key** (starts with `sk_test_` for testing, `sk_live_` for production)
4. Add to `.env`: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
5. Start the booking server:
   ```bash
   python tools/run_api_server.py
   ```
6. Set up webhook:
   - Stripe Dashboard -> Developers -> Webhooks -> Add endpoint
   - URL: `https://YOUR_SERVER/api/webhook`
   - Events: `checkout.session.completed`
   - Copy signing secret -> add as `STRIPE_WEBHOOK_SECRET`

**Testing**: Use card `4242 4242 4242 4242`, any future date, any CVV.

---

## Step 6 — Deploy to Railway

```bash
railway up
```

After deploy, update `BOOKING_API_URL` in `.env` to the Railway URL.

---

## Step 7 — MCP Server (connect to Claude Desktop / Claude Code)

The MCP server exposes slot inventory as AI-callable tools.

### Connect to Claude Desktop:

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

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

### Claude Code (already configured):
`~/.claude/settings.json` has been updated. Claude Code can call:
- `search_last_minute_slots` — find slots by city / category / time
- `book_slot` — execute booking (returns checkout URL or confirmation)
- `get_booking_status` — check booking by booking_id

---

## Notes
- `.env` is gitignored — never commit it
- `tools/seeds/octo_suppliers.json` controls which OCTO suppliers are active
- Booking confirmations saved to `.tmp/booking_confirmations/` for debugging
