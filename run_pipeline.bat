@echo off
REM ============================================================
REM run_pipeline.bat — Main pipeline runner (no LLM calls)
REM Scheduled via Windows Task Scheduler every 4 hours.
REM ============================================================

cd /d "%~dp0"
set PYTHON=python

echo [%date% %time%] Pipeline starting...

REM ── 1. Fetch slots from all platforms ───────────────────────

REM OCTO-compliant platforms (Ventrata, Bokun, Peek Pro, etc.) — set api keys in .env
echo Fetching OCTO slots (Ventrata, Bokun, Peek Pro, Xola, etc.)...
if exist tools\fetch_octo_slots.py (
    %PYTHON% tools/fetch_octo_slots.py --hours-ahead 72
)

REM Rezdy Agent API — free, 48h approval at rezdy.com
echo Fetching Rezdy activity slots...
if exist tools\fetch_rezdy_slots.py (
    %PYTHON% tools/fetch_rezdy_slots.py --hours-ahead 72
)

echo Fetching Mindbody slots...
%PYTHON% tools/fetch_mindbody_slots.py --hours-ahead 72 --max-cities 50

echo Fetching Eventbrite slots (all 154 cities, 5 pages each)...
if exist tools\fetch_eventbrite_slots.py (
    %PYTHON% tools/fetch_eventbrite_slots.py --hours-ahead 72 --max-cities 154 --max-pages 5
)

echo Fetching Meetup events (all 154 cities, 3 pages each)...
if exist tools\fetch_meetup_slots.py (
    %PYTHON% tools/fetch_meetup_slots.py --hours-ahead 72 --max-cities 154 --max-pages 3
)

echo Fetching Ticketmaster events (all 154 cities)...
if exist tools\fetch_ticketmaster_slots.py (
    %PYTHON% tools/fetch_ticketmaster_slots.py --hours-ahead 72 --max-cities 154
)

echo Fetching SeatGeek events (all 154 cities)...
if exist tools\fetch_seatgeek_slots.py (
    %PYTHON% tools/fetch_seatgeek_slots.py --hours-ahead 72 --max-cities 154
)

echo Fetching Luma events (20 cities)...
if exist tools\fetch_luma_slots.py (
    %PYTHON% tools/fetch_luma_slots.py --hours-ahead 72
)

echo Fetching Dice.fm events (30 cities)...
if exist tools\fetch_dice_slots.py (
    %PYTHON% tools/fetch_dice_slots.py --hours-ahead 72 --max-cities 30
)

echo Fetching Booksy beauty/wellness slots (30 cities)...
if exist tools\fetch_booksy_slots.py (
    %PYTHON% tools/fetch_booksy_slots.py --hours-ahead 72 --max-cities 30
)

echo Fetching FareHarbor activity slots...
if exist tools\fetch_fareharbor_slots.py (
    %PYTHON% tools/fetch_fareharbor_slots.py --hours-ahead 72 --max-companies 100
)

REM Airbnb iCal requires auth — skip until re-enabled
REM %PYTHON% tools/fetch_airbnb_ical_slots.py --mode slots --hours-ahead 72

REM ── 2. Aggregate + deduplicate ──────────────────────────────
echo Aggregating slots...
%PYTHON% tools/aggregate_slots.py --hours-ahead 72
if errorlevel 1 (
    echo ERROR: Aggregation failed. Aborting pipeline.
    exit /b 1
)

REM ── 3. Enrich missing prices ────────────────────────────────
echo Enriching prices...
if exist tools\enrich_prices.py (
    %PYTHON% tools/enrich_prices.py --max-eventbrite 500 --delay 0.6
)

REM ── 4. Compute pricing ──────────────────────────────────────
echo Computing pricing...
%PYTHON% tools/compute_pricing.py
if errorlevel 1 (
    echo ERROR: Pricing failed. Aborting pipeline.
    exit /b 1
)

REM ── 5. Generate affiliate links ─────────────────────────────
echo Generating affiliate links...
%PYTHON% tools/generate_affiliate_links.py

REM ── 6. Write to Google Sheets ───────────────────────────────
echo Writing to Google Sheets...
%PYTHON% tools/write_to_sheets.py
if errorlevel 1 (
    echo ERROR: Sheets write failed.
)

REM ── 7. Sync to Supabase ─────────────────────────────────────
echo Syncing to Supabase...
%PYTHON% tools/sync_to_supabase.py

REM ── 8. Rebuild landing page ─────────────────────────────────
echo Rebuilding landing page...
%PYTHON% tools/update_landing_page.py

REM ── 9. Distribute deals ─────────────────────────────────────
echo Distributing deals...
if exist tools\post_to_telegram.py (
    %PYTHON% tools/post_to_telegram.py
)
if exist tools\post_to_twitter.py (
    %PYTHON% tools/post_to_twitter.py
)
if exist tools\post_to_reddit.py (
    %PYTHON% tools/post_to_reddit.py
)

REM ── 10. SMS alerts (requires Twilio in .env) ────────────────────────────
if exist tools\send_sms_alert.py (
    %PYTHON% tools/send_sms_alert.py
)

REM ── 11. Notify webhook subscribers ─────────────────────────────────────────
if exist tools\notify_webhooks.py (
    %PYTHON% tools/notify_webhooks.py
)

REM ── 12. Run watcher one-shot to catch any slots from continuous platforms ──
if exist tools\watch_slots_realtime.py (
    %PYTHON% tools/watch_slots_realtime.py --once
)

REM ── 13. Refresh market insights snapshot ──────────────────────────────────
if exist tools\market_insights.py (
    %PYTHON% tools/market_insights.py refresh
)

echo [%date% %time%] Pipeline complete.
