# Workflow: Discover Mindbody Slots

## Objective
Fetch all open booking slots within 72 hours from Mindbody-powered businesses
(yoga studios, massage studios, fitness centers, spas, etc.) using the Mindbody Open API.

## Tool
`tools/fetch_mindbody_slots.py`

## Required Inputs
- `MINDBODY_API_KEY` in `.env`
- `MINDBODY_SITE_IDS` in `.env` (comma-separated site IDs) OR `--location-ids` CLI arg

## Expected Output
`.tmp/mindbody_slots.json` — list of normalized slot records

---

## How to Run

```bash
python tools/fetch_mindbody_slots.py --hours-ahead 72
```

Or with explicit site IDs:
```bash
python tools/fetch_mindbody_slots.py --location-ids 123456,789012 --hours-ahead 72
```

---

## What the Tool Does

1. For each site ID:
   - Calls `/class/classes` to get open group class slots
   - Calls `/appointment/staffavailabilities` to get open 1-on-1 appointment slots
   - Filters to the 72-hour window
   - Normalizes each slot to the canonical schema (via `normalize_slot.py`)
   - Skips cancelled, full, or past slots
2. Writes all valid slots to `.tmp/mindbody_slots.json`
3. Prints a count of valid slots and any errors

---

## API Rate Limits

- Free tier: 1,000 requests/day
- This tool makes ~2-4 requests per site ID per run
- Running every 4 hours with 10 site IDs ≈ 60 requests/day — well within limits
- On 429 (rate limit): the tool backs off 60s and retries once automatically
- If you see repeated 429s, reduce the number of site IDs or run less frequently

---

## Error Handling

| Error | Cause | Action |
|---|---|---|
| 401 Unauthorized | Invalid or expired API key | Regenerate key at developers.mindbodyonline.com |
| 404 Not Found | Site ID doesn't exist or has no API access | Remove the site ID from MINDBODY_SITE_IDS |
| 429 Too Many Requests | Rate limit hit | Tool retries once after 60s; if persistent, reduce run frequency |
| 503 Service Unavailable | Mindbody API down | Skip gracefully; re-run on next cycle |
| Appointments endpoint 4xx | Site doesn't have appointments module | Tool logs and continues — classes are still fetched |

---

## How to Find Site IDs

Mindbody site IDs appear in booking URLs:
- `https://www.mindbodyonline.com/explore/site/123456` → site ID = `123456`
- Search Mindbody's business directory at [mindbodyonline.com/explore](https://www.mindbodyonline.com/explore)
- Filter by city and category to find active studios

Start with 5-10 site IDs in your target city to validate the pipeline. Expand from there.

---

## Known Quirks

- The classes endpoint returns `IsCanceled` and `IsAvailable` separately — check both
- Appointment pricing requires a separate API call; the tool leaves `price=None` for appointments
  (compute_pricing.py uses a floor markup when price is unknown — fix this by adding pricing calls if needed)
- Some sites expose classes but not appointments (different subscription tiers)
- Site IDs may change if a business re-registers — monitor for 404s
- `StartDateTime` and `EndDateTime` from Mindbody are in the studio's local timezone, not UTC
  — the normalization step converts these; verify timezone handling if bookings are off by hours
