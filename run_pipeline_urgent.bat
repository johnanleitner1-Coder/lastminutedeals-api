@echo off
REM ============================================================
REM run_pipeline_urgent.bat — Fast 30-minute cycle for sub-12h slots
REM Only re-aggregates, reprices, resyncs, and redeploys.
REM Does NOT re-fetch from platforms (too slow for 30min cycle).
REM ============================================================

cd /d "%~dp0"
set PYTHON=python

echo [%date% %time%] Urgent pipeline starting...

REM Aggregate existing fetched data (fast — local files only)
%PYTHON% tools/aggregate_slots.py --hours-ahead 12
if errorlevel 1 exit /b 1

REM Reprice (urgency multipliers change fast for sub-12h slots)
%PYTHON% tools/compute_pricing.py

REM Sync to Supabase
%PYTHON% tools/sync_to_supabase.py

REM Rebuild landing page
%PYTHON% tools/update_landing_page.py

REM Notify webhooks
if exist tools\notify_webhooks.py (
    %PYTHON% tools/notify_webhooks.py
)

echo [%date% %time%] Urgent pipeline complete.
