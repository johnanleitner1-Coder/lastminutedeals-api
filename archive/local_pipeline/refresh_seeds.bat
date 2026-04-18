@echo off
REM ============================================================
REM refresh_seeds.bat — Refresh supplier seed data (run weekly)
REM Currently only updates OCTO supplier product lists.
REM ============================================================

cd /d "%~dp0"
set PYTHON=python

echo [%date% %time%] Seed refresh starting...

echo Refreshing OCTO supplier product catalog...
%PYTHON% tools/fetch_octo_slots.py --hours-ahead 168

echo [%date% %time%] Seed refresh complete.
