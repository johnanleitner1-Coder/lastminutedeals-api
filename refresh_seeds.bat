@echo off
REM ============================================================
REM refresh_seeds.bat — Refresh platform seed data (run weekly)
REM Expands listing IDs, subreddit lists, etc.
REM ============================================================

cd /d "%~dp0"
set PYTHON=python

echo [%date% %time%] Seed refresh starting...

echo Refreshing Airbnb listing IDs (all cities)...
%PYTHON% tools/fetch_airbnb_ical_slots.py --mode seed --max-cities 160

echo [%date% %time%] Seed refresh complete.
