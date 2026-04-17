# Workflow: Run Slot Discovery (Master Orchestrator)

## Objective
Run the complete pipeline end-to-end: fetch open slots from all configured OCTO/Bokun
suppliers, aggregate, price, and write to Google Sheets. Optionally trigger distribution
agents. Runs on a 4-hour schedule.

## Schedule
Every 4 hours via Windows Task Scheduler (see setup step below).

---

## Pipeline Steps

Run each step in sequence. If a step fails, log the error and continue where possible -
partial results are better than no results.

### Step 1 - Health check (optional but recommended)
```bash
python tools/check_api_health.py
```
If a platform reports DOWN, skip its fetch tool. Log the failure in the run summary.

### Step 2 - Fetch slots (OCTO platforms)
```bash
python tools/fetch_octo_slots.py --hours-ahead 168
```
Fetches from all configured suppliers in `tools/seeds/octo_suppliers.json`.
Output: `.tmp/octo_slots.json`

### Step 3 - Aggregate
```bash
python tools/aggregate_slots.py --hours-ahead 168
```
Reads all `.tmp/*_slots.json`, deduplicates on `slot_id`, filters to window, sorts by urgency.
Output: `.tmp/aggregated_slots.json`

### Step 4 - Compute pricing
```bash
python tools/compute_pricing.py
```
Sets `our_price` and `our_markup` for each slot using urgency + supply + historical fill rate.
Modifies `aggregated_slots.json` in-place.

### Step 5 - Generate affiliate links
```bash
python tools/generate_affiliate_links.py
```
Adds `affiliate_url` to each slot. Requires affiliate IDs in `.env`.
Modifies `aggregated_slots.json` in-place. (OK to skip if no IDs configured yet.)

### Step 6 - Write to Google Sheets
```bash
python tools/write_to_sheets.py
```
Upserts slots to "Slots" tab, expires stale slots, appends run log entry.

### Step 7 - Sync to Supabase
```bash
python tools/sync_to_supabase.py
```
Upserts slots to the Supabase `slots` table for the API server and MCP server.

### Step 8 - Update landing page
```bash
python tools/update_landing_page.py
```
Regenerates the static HTML from current slots and deploys to Netlify/GitHub Pages.

### Step 9 - Distribute deals
```bash
python tools/post_to_twitter.py
python tools/post_to_reddit.py
python tools/post_to_telegram.py
```
Each tool reads `aggregated_slots.json`, checks which slots are new since last post,
and distributes to its channel. Skips if no new slots.

---

## Error Recovery

| Failure | Action |
|---|---|
| Fetch tool crashes | Log the error; note in RunLog |
| Aggregate produces 0 results | Log warning; skip Sheets write; don't post empty deals |
| Sheets write fails | Retry once with 30s delay; if still fails, save slots to `.tmp/failed_write.json` |
| Distribution tool fails | Log the error; skip that channel for this cycle; retry next cycle |
| API key expired | Alert in RunLog; continue without that platform |

---

## Running as a Scheduled Task (Windows Task Scheduler)

1. Open Task Scheduler -> Create Basic Task
2. Name: "Last Minute Deals - Slot Discovery"
3. Trigger: Daily, repeat every 4 hours
4. Action: Start a Program
   - Program: `python`
   - Arguments: `"c:/Users/janaa/Agentic Workflows/tools/run_pipeline.py"`
   - Start in: `c:/Users/janaa/Agentic Workflows`

Or run the steps directly using a batch file:
```batch
@echo off
cd /d "c:\Users\janaa\Agentic Workflows"
python tools\fetch_octo_slots.py --hours-ahead 168
python tools\aggregate_slots.py
python tools\compute_pricing.py
python tools\generate_affiliate_links.py
python tools\write_to_sheets.py
python tools\sync_to_supabase.py
```

---

## Monitoring

After each run, check:
1. Google Sheets "RunLog" tab - verify `slots_new > 0` at least once per day
2. Google Sheets "Slots" tab - verify `hours_until_start` values are current
3. If `slots_new = 0` for 3 consecutive runs: check API health, verify supplier config

---

## What Good Output Looks Like

A healthy run produces:
```
Fetching OCTO slots | suppliers=4 | window=168h
  Arctic Adventures: 12 products, 45 availability slots
  Bicycle Roma: 3 products, 18 availability slots

Aggregation complete
  Raw records     : 63
  Duplicates      : 0
  Kept (output)   : 63

Pricing complete: 63 priced, 0 without original price

Sheets write complete
  New     : 15
  Updated : 48
  Expired : 3
```
