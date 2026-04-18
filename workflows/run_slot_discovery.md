# Workflow: Run Slot Discovery (Master Orchestrator)

## Objective
Run the complete pipeline end-to-end: fetch open slots from all configured OCTO/Bokun
suppliers, aggregate, price, and sync to Supabase. Runs on a 4-hour schedule.

## Schedule
Every 4 hours via Railway scheduler (run_api_server.py APScheduler job).
Can also be run locally via `run_pipeline.bat`.

---

## Pipeline Steps

Run each step in sequence. If a step fails, log the error and continue where possible —
partial results are better than no results.

### Step 1 - Fetch slots
```bash
python tools/fetch_octo_slots.py --hours-ahead 168
```
Fetches from all configured suppliers in `tools/seeds/octo_suppliers.json`.
Output: `.tmp/octo_slots.json`

### Step 2 - Aggregate
```bash
python tools/aggregate_slots.py --hours-ahead 168
```
Reads `.tmp/octo_slots.json`, deduplicates on `slot_id`, filters to window, sorts by urgency.
Output: `.tmp/aggregated_slots.json`

### Step 3 - Compute pricing
```bash
python tools/compute_pricing.py
```
Sets `our_price` and `our_markup` for each slot using urgency + supply + historical fill rate.
Modifies `aggregated_slots.json` in-place.

### Step 4 - Sync to Supabase
```bash
python tools/sync_to_supabase.py
```
Upserts slots to the Supabase `slots` table. This is what the API server and MCP server read.

---

## Error Recovery

| Failure | Action |
|---|---|
| Fetch tool crashes | Log the error; continue with existing data |
| Aggregate produces 0 results | Log warning; skip Supabase sync |
| Supabase sync fails | Retry once with 30s delay; if still fails, save slots to `.tmp/failed_write.json` |
| API key expired | Log warning; continue without that supplier |

---

## Running Locally

```batch
@echo off
cd /d "c:\Users\janaa\Agentic Workflows"
python tools\fetch_octo_slots.py --hours-ahead 168
python tools\aggregate_slots.py --hours-ahead 168
python tools\compute_pricing.py
python tools\sync_to_supabase.py
```

Or use `run_pipeline.bat`.

---

## What Good Output Looks Like

A healthy run produces:
```
Fetching OCTO slots | suppliers=16 | window=168h
  Arctic Adventures: 12 products, 45 availability slots
  Bicycle Roma: 3 products, 18 availability slots
  ...

Aggregation complete
  Raw records     : 5150
  Duplicates      : 0
  Kept (output)   : 5150

Pricing complete: 5150 priced, 0 without original price

Supabase sync complete: 5150 upserted
```
