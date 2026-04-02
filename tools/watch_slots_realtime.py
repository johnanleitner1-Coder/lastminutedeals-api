"""
watch_slots_realtime.py — Continuous real-time slot watcher for LastMinuteDeals.

Replaces the 30-minute polling cycle with sub-minute continuous watchers that
detect new slots the instant they appear on source platforms.

Architecture:
  - One watcher thread per platform
  - Each thread polls its platform API/feed on a configurable interval (default 45s)
  - When new slots detected: updates aggregated_slots.json, re-runs pricing, fires webhooks
  - Writes watcher_status.json so the dashboard can show live freshness

Supported watch targets (fast, API-based — no Playwright needed):
  eventbrite    ~45s   REST API, no login required
  luma          ~45s   public GraphQL
  meetup        ~60s   public GraphQL
  seatgeek      ~30s   public REST API

Usage:
  python tools/watch_slots_realtime.py                          # watch all
  python tools/watch_slots_realtime.py --platforms eventbrite luma
  python tools/watch_slots_realtime.py --interval 30            # 30s per platform
  python tools/watch_slots_realtime.py --once                   # run one cycle then exit (cron-friendly)

Required .env:
  EVENTBRITE_API_KEY     (for eventbrite watcher)
  SEATGEEK_CLIENT_ID     (for seatgeek watcher)
  SUPABASE_URL           (for pushing new slots)
  SUPABASE_SECRET_KEY    (for pushing new slots)
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

ROOT        = Path(__file__).parent.parent
TOOLS_DIR   = Path(__file__).parent
TMP_DIR     = ROOT / ".tmp"
AGG_FILE    = TMP_DIR / "aggregated_slots.json"
DELTA_DIR   = TMP_DIR / "watcher_deltas"
STATUS_FILE = TMP_DIR / "watcher_status.json"

TMP_DIR.mkdir(exist_ok=True)
DELTA_DIR.mkdir(exist_ok=True)

DEFAULT_INTERVAL = 45  # seconds between polls per platform
HOURS_AHEAD      = 72  # only care about slots within this window


# ── Status tracking ───────────────────────────────────────────────────────────

_status: dict = {}
_status_lock = threading.Lock()

def _update_status(platform: str, **kwargs):
    with _status_lock:
        if platform not in _status:
            _status[platform] = {}
        _status[platform].update(kwargs)
        _status[platform]["updated_at"] = datetime.now(timezone.utc).isoformat()
        STATUS_FILE.write_text(json.dumps(_status, indent=2), encoding="utf-8")


# ── Slot merging ─────────────────────────────────────────────────────────────

_agg_lock = threading.Lock()

def _load_agg() -> list:
    if AGG_FILE.exists():
        try:
            return json.loads(AGG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def _save_agg(slots: list):
    AGG_FILE.write_text(json.dumps(slots, indent=2), encoding="utf-8")

def _merge_new_slots(new_slots: list) -> list:
    """Merge new slots into aggregated_slots.json. Returns only truly new slots."""
    if not new_slots:
        return []

    with _agg_lock:
        existing = _load_agg()
        existing_ids = {s.get("slot_id") for s in existing}
        now = datetime.now(timezone.utc)

        added = []
        existing_map = {s.get("slot_id"): s for s in existing}

        for slot in new_slots:
            sid = slot.get("slot_id")
            if not sid:
                continue
            # Recompute hours_until_start
            try:
                st = datetime.fromisoformat(slot.get("start_time", "").replace("Z", "+00:00"))
                slot["hours_until_start"] = round((st - now).total_seconds() / 3600, 2)
            except Exception:
                pass

            if sid not in existing_ids:
                added.append(slot)
            else:
                # Update existing slot's freshness data
                existing_map[sid].update({
                    "hours_until_start": slot.get("hours_until_start"),
                    "scraped_at": slot.get("scraped_at"),
                })

        if added:
            all_slots = list(existing_map.values()) + added
            # Filter out expired
            fresh = []
            for s in all_slots:
                h = s.get("hours_until_start")
                if h is not None and h < 0:
                    continue
                if h is not None and h > HOURS_AHEAD:
                    continue
                fresh.append(s)
            fresh.sort(key=lambda s: s.get("hours_until_start") or 999)
            _save_agg(fresh)

        return added


# ── Post-discovery actions ────────────────────────────────────────────────────

def _run_tool(script_name: str, *args):
    """Run a tool script as a subprocess."""
    script = TOOLS_DIR / script_name
    if not script.exists():
        print(f"[WATCHER] Tool not found, skipping: {script_name}")
        return
    cmd = [sys.executable, str(script)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"[WATCHER] {script_name} error: {result.stderr[:200]}")
    else:
        if result.stdout.strip():
            print(f"[WATCHER] {script_name}: {result.stdout.strip()[:150]}")

def _on_new_slots(new_slots: list, platform: str):
    """Called whenever genuinely new slots are found. Runs downstream actions."""
    count = len(new_slots)
    print(f"[WATCHER] {platform}: {count} new slot(s) detected — running downstream pipeline")

    # Write delta file for audit
    delta_path = DELTA_DIR / f"{platform}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    delta_path.write_text(json.dumps(new_slots, indent=2), encoding="utf-8")

    # Run pricing on the whole aggregated file
    _run_tool("compute_pricing.py")

    # Push to Supabase
    _run_tool("sync_to_supabase.py")

    # Fire webhooks for subscribers who are waiting for these slots
    _run_tool("notify_webhooks.py")

    print(f"[WATCHER] {platform}: downstream pipeline complete for {count} new slot(s)")


# ── Platform watchers ─────────────────────────────────────────────────────────

def _load_fetcher(module_name: str):
    """Dynamically load a fetch_*.py module to call its fetch function."""
    path = TOOLS_DIR / f"{module_name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _watch_eventbrite(interval: int, stop_event: threading.Event):
    """Poll Eventbrite public API for new last-minute events."""
    platform = "eventbrite"
    api_key = os.getenv("EVENTBRITE_API_KEY", "")
    if not api_key:
        _update_status(platform, state="disabled", reason="EVENTBRITE_API_KEY not set")
        print(f"[WATCHER] {platform}: skipped (no API key)")
        return

    import requests as req

    _update_status(platform, state="running", interval_s=interval)
    seen_ids: set = set()

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            end_dt = now + timedelta(hours=HOURS_AHEAD)

            url = "https://www.eventbriteapi.com/v3/events/search/"
            params = {
                "start_date.range_start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "start_date.range_end":   end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sort_by":  "date",
                "page_size": 50,
                "expand":   "venue,ticket_classes",
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = req.get(url, params=params, headers=headers, timeout=15)

            if resp.status_code == 200:
                events = resp.json().get("events", [])
                new_slots = []

                mod = _load_fetcher("fetch_eventbrite_slots")
                for ev in events:
                    eid = ev.get("id", "")
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    if mod and hasattr(mod, "_normalize_event"):
                        slot = mod._normalize_event(ev, now)
                        if slot:
                            new_slots.append(slot)

                added = _merge_new_slots(new_slots)
                _update_status(platform, state="running", last_poll=now.isoformat(),
                               new_this_cycle=len(added), seen_total=len(seen_ids))
                if added:
                    _on_new_slots(added, platform)
            else:
                _update_status(platform, state="error", http_status=resp.status_code)

        except Exception as e:
            _update_status(platform, state="error", error=str(e)[:200])
            print(f"[WATCHER] {platform} error: {e}")

        stop_event.wait(interval)


def _watch_luma(interval: int, stop_event: threading.Event):
    """Poll lu.ma public events API for last-minute slots."""
    platform = "luma"
    import requests as req

    _update_status(platform, state="running", interval_s=interval)
    seen_ids: set = set()

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            end_dt = now + timedelta(hours=HOURS_AHEAD)

            # Luma public list endpoint
            url = "https://api.lu.ma/public/v1/event/get-by-series"
            list_url = "https://api.lu.ma/discover"
            # Use the calendar discover endpoint
            resp = req.get(
                "https://api.lu.ma/public/v2/event/list",
                params={
                    "after":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "before": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "pagination_limit": 50,
                },
                timeout=15,
            )

            new_slots = []
            if resp.status_code == 200:
                data = resp.json()
                events = data.get("entries", []) or data.get("events", [])
                mod = _load_fetcher("fetch_luma_slots")

                for ev in events:
                    eid = ev.get("api_id") or ev.get("id", "")
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    if mod and hasattr(mod, "_normalize_event"):
                        slot = mod._normalize_event(ev, now)
                        if slot:
                            new_slots.append(slot)

            added = _merge_new_slots(new_slots)
            _update_status(platform, state="running", last_poll=now.isoformat(),
                           new_this_cycle=len(added), seen_total=len(seen_ids))
            if added:
                _on_new_slots(added, platform)

        except Exception as e:
            _update_status(platform, state="error", error=str(e)[:200])
            print(f"[WATCHER] {platform} error: {e}")

        stop_event.wait(interval)


def _watch_seatgeek(interval: int, stop_event: threading.Event):
    """Poll SeatGeek API for last-minute event tickets."""
    platform = "seatgeek"
    client_id = os.getenv("SEATGEEK_CLIENT_ID", "")
    if not client_id:
        _update_status(platform, state="disabled", reason="SEATGEEK_CLIENT_ID not set")
        print(f"[WATCHER] {platform}: skipped (no client ID)")
        return

    import requests as req

    _update_status(platform, state="running", interval_s=interval)
    seen_ids: set = set()

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            end_dt = now + timedelta(hours=HOURS_AHEAD)

            resp = req.get(
                "https://api.seatgeek.com/2/events",
                params={
                    "client_id":            client_id,
                    "datetime_local.gte":   now.strftime("%Y-%m-%dT%H:%M:%S"),
                    "datetime_local.lte":   end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sort":  "datetime_local.asc",
                    "per_page": 50,
                },
                timeout=15,
            )

            new_slots = []
            if resp.status_code == 200:
                events = resp.json().get("events", [])
                mod = _load_fetcher("fetch_seatgeek_slots")

                for ev in events:
                    eid = str(ev.get("id", ""))
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    if mod and hasattr(mod, "_normalize_event"):
                        slot = mod._normalize_event(ev, now)
                        if slot:
                            new_slots.append(slot)

            added = _merge_new_slots(new_slots)
            _update_status(platform, state="running", last_poll=now.isoformat(),
                           new_this_cycle=len(added), seen_total=len(seen_ids))
            if added:
                _on_new_slots(added, platform)

        except Exception as e:
            _update_status(platform, state="error", error=str(e)[:200])
            print(f"[WATCHER] {platform} error: {e}")

        stop_event.wait(interval)


def _watch_meetup(interval: int, stop_event: threading.Event):
    """Poll Meetup GraphQL API for last-minute events."""
    platform = "meetup"
    import requests as req

    _update_status(platform, state="running", interval_s=interval)
    seen_ids: set = set()

    query = """
    query LastMinuteEvents($after: ZonedDateTime!, $before: ZonedDateTime!) {
      rankedEvents(filter: {startDateRange: {start: $after, end: $before}}, first: 50) {
        edges {
          node {
            id title eventUrl dateTime venue { city state country }
            group { name }
            maxTickets rsvpState
          }
        }
      }
    }
    """

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            end_dt = now + timedelta(hours=HOURS_AHEAD)

            resp = req.post(
                "https://api.meetup.com/gql",
                json={
                    "query": query,
                    "variables": {
                        "after":  now.strftime("%Y-%m-%dT%H:%M:%S"),
                        "before": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                },
                headers={"Content-Type": "application/json"},
                timeout=15,
            )

            new_slots = []
            if resp.status_code == 200:
                edges = (resp.json().get("data") or {}).get("rankedEvents", {}).get("edges", [])
                mod = _load_fetcher("fetch_meetup_slots")

                for edge in edges:
                    node = edge.get("node", {})
                    eid  = str(node.get("id", ""))
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    if mod and hasattr(mod, "_normalize_event"):
                        slot = mod._normalize_event(node, now)
                        if slot:
                            new_slots.append(slot)

            added = _merge_new_slots(new_slots)
            _update_status(platform, state="running", last_poll=now.isoformat(),
                           new_this_cycle=len(added), seen_total=len(seen_ids))
            if added:
                _on_new_slots(added, platform)

        except Exception as e:
            _update_status(platform, state="error", error=str(e)[:200])
            print(f"[WATCHER] {platform} error: {e}")

        stop_event.wait(interval)


# ── Watcher registry ──────────────────────────────────────────────────────────

WATCHERS = {
    "eventbrite": _watch_eventbrite,
    "luma":       _watch_luma,
    "seatgeek":   _watch_seatgeek,
    "meetup":     _watch_meetup,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def run(platforms: list[str] | None = None, interval: int = DEFAULT_INTERVAL, once: bool = False):
    """
    Start watchers for all (or specified) platforms.

    once=True: run exactly one poll cycle per platform then exit (useful for cron).
    """
    targets = platforms if platforms else list(WATCHERS.keys())
    print(f"[WATCHER] Starting real-time watchers: {', '.join(targets)} (interval={interval}s)")

    if once:
        # Run each watcher synchronously for one cycle
        for name in targets:
            fn = WATCHERS.get(name)
            if not fn:
                print(f"[WATCHER] Unknown platform: {name}")
                continue
            stop = threading.Event()
            stop.set()  # stops after first iteration waits
            _update_status(name, state="one-shot")
            # Run in a thread so stop_event works correctly
            t = threading.Thread(target=fn, args=(0, stop), daemon=True)
            t.start()
            t.join(timeout=60)
        print("[WATCHER] One-shot cycle complete.")
        return

    # Continuous mode — one thread per platform
    stop_event = threading.Event()
    threads = []

    for name in targets:
        fn = WATCHERS.get(name)
        if not fn:
            print(f"[WATCHER] Unknown platform: {name}")
            continue
        t = threading.Thread(target=fn, args=(interval, stop_event), daemon=True, name=f"watcher-{name}")
        t.start()
        threads.append(t)
        print(f"[WATCHER] Started thread: watcher-{name}")

    print(f"[WATCHER] All {len(threads)} watcher(s) running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(10)
            alive = sum(1 for t in threads if t.is_alive())
            if alive == 0:
                print("[WATCHER] All watcher threads stopped.")
                break
    except KeyboardInterrupt:
        print("\n[WATCHER] Stopping all watchers...")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
        print("[WATCHER] Stopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real-time slot watcher")
    parser.add_argument("--platforms", nargs="+", choices=list(WATCHERS.keys()),
                        help="Platforms to watch (default: all)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--once", action="store_true",
                        help="Run one poll cycle then exit (cron-friendly)")
    args = parser.parse_args()

    run(platforms=args.platforms, interval=args.interval, once=args.once)
