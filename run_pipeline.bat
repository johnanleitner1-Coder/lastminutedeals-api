@echo off
cd /d "c:\Users\janaa\Agentic Workflows"
python tools\fetch_octo_slots.py
python tools\aggregate_slots.py
python tools\compute_pricing.py
python tools\sync_to_supabase.py
python tools\update_landing_page.py
