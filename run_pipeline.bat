@echo off
cd /d "c:\Users\janaa\Agentic Workflows"
python tools\fetch_octo_slots.py --hours-ahead 168
python tools\aggregate_slots.py --hours-ahead 168
python tools\compute_pricing.py
python tools\sync_to_supabase.py
