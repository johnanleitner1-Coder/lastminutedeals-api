# Archived: Local Pipeline Scripts

**Archived:** 2026-04-18
**Reason:** Railway now runs the full pipeline autonomously every 4 hours via APScheduler in `run_api_server.py`. These local scripts are redundant and were actively causing problems — the local APScheduler and Task Scheduler jobs overwrote `.tmp/aggregated_slots.json` with partial/filtered results, conflicting with manual pipeline runs.

## What was disabled

### Windows Task Scheduler jobs (all DISABLED, not deleted)
- `LastMinuteDeals Pipeline` — ran run_pipeline.bat every ~2h
- `LastMinuteDeals-Pipeline` — duplicate of above, every ~4h
- `LastMinuteDeals_Pipeline` — another duplicate, every ~4h
- `LastMinuteDeals Urgent Pipeline` — ran run_pipeline_urgent.bat every 30 min
- `LastMinuteDeals_SeedRefresh` — ran refresh_seeds.bat weekly

### Local API server
The local instance of `run_api_server.py` (port 5050) was also running the pipeline via its own APScheduler. This server should NOT be started locally — Railway is the production server.

## What replaced them

Railway's `run_api_server.py` runs `_run_slot_discovery()` every 4 hours:
- fetch_octo_slots.py (all 16 vendors, vendor-scoped tokens)
- aggregate_slots.py (168h window)
- compute_pricing.py (dynamic markup)
- sync_to_supabase.py (upsert to production DB)

## If you need to run the pipeline manually

Run the scripts directly (no batch file or server needed):
```
cd "c:\Users\janaa\Agentic Workflows"
python tools/fetch_octo_slots.py --hours-ahead 168
python tools/aggregate_slots.py --hours-ahead 168
python tools/compute_pricing.py
python tools/sync_to_supabase.py
```

## To re-enable (if ever needed)
```
schtasks /Change /TN "LastMinuteDeals_Pipeline" /Enable
```
