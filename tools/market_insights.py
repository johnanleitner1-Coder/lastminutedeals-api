"""
market_insights.py — Market intelligence engine for LastMinuteDeals.

Tracks and exposes:
  - Booking success rates per platform
  - Slot fill velocity (how fast slots sell out)
  - Price elasticity per category/city (markup vs conversion)
  - Optimal booking windows (when to book for max savings)
  - Platform reliability scores
  - Supply/demand balance per city + category

Data is collected passively from every booking attempt and every pipeline run.
This data compounds over time and creates switching costs — agents that leave
lose historical intelligence that took months to accumulate.

Storage:
  .tmp/insights/booking_outcomes.jsonl  — one record per booking attempt
  .tmp/insights/slot_observations.jsonl — one record per slot observed (price + fill status)
  .tmp/insights/market_snapshot.json    — pre-computed aggregates (refreshed on each pipeline run)

Usage:
  from tools.market_insights import record_booking_outcome, record_slot_observation, get_market_snapshot

  # Record a booking attempt result (called by execution_engine.py):
  record_booking_outcome(platform="eventbrite", category="entertainment", city="Chicago",
                         success=True, attempts=2, price=45.0, hours_before_start=6.5)

  # Get insights for a specific market:
  snapshot = get_market_snapshot(category="wellness", city="New York")

  # Full market overview for /insights/market endpoint:
  overview = build_market_overview()

CLI:
  python tools/market_insights.py refresh         # rebuild market_snapshot.json from raw data
  python tools/market_insights.py show            # print current snapshot
  python tools/market_insights.py show --category wellness --city NYC
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT         = Path(__file__).parent.parent
INSIGHTS_DIR = ROOT / ".tmp" / "insights"
OUTCOMES_FILE   = INSIGHTS_DIR / "booking_outcomes.jsonl"
OBSERVATIONS_FILE = INSIGHTS_DIR / "slot_observations.jsonl"
SNAPSHOT_FILE = INSIGHTS_DIR / "market_snapshot.json"

INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8")


# ── Event recording ───────────────────────────────────────────────────────────

def record_booking_outcome(
    platform: str,
    category: str,
    city: str,
    success: bool,
    attempts: int,
    price: float,
    hours_before_start: float,
    slot_id: str = "",
    error_type: str = "",
):
    """
    Append one booking outcome record. Called after every execution attempt.
    Each line is a JSON object — append-only for durability.
    """
    record = {
        "ts":                  datetime.now(timezone.utc).isoformat(),
        "platform":            platform,
        "category":            category,
        "city":                city,
        "success":             success,
        "attempts":            attempts,
        "price":               price,
        "hours_before_start":  hours_before_start,
        "slot_id":             slot_id,
        "error_type":          error_type,
    }
    with open(OUTCOMES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_slot_observation(slot: dict):
    """
    Record a slot being observed (with or without price). Called by aggregate_slots.py.
    Used to compute fill velocity: slots that appear and then disappear = sold out.
    """
    record = {
        "ts":               datetime.now(timezone.utc).isoformat(),
        "slot_id":          slot.get("slot_id", ""),
        "platform":         slot.get("platform", ""),
        "category":         slot.get("category", ""),
        "city":             slot.get("location_city", ""),
        "price":            slot.get("our_price") or slot.get("price"),
        "hours_until_start": slot.get("hours_until_start"),
        "status":           "available",
    }
    with open(OBSERVATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_slot_booked(slot_id: str, platform: str, category: str, city: str, price: float):
    """Mark a slot as booked in observations (for fill velocity calculation)."""
    record = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "slot_id": slot_id,
        "platform": platform,
        "category": category,
        "city":     city,
        "price":    price,
        "status":   "booked",
    }
    with open(OBSERVATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ── Aggregation ───────────────────────────────────────────────────────────────

def _load_outcomes(days_back: int = 90) -> list[dict]:
    """Load booking outcome records from the last N days."""
    if not OUTCOMES_FILE.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    records = []
    with open(OUTCOMES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("ts", "") >= cutoff:
                    records.append(r)
            except Exception:
                pass
    return records


def _load_observations(days_back: int = 30) -> list[dict]:
    """Load slot observations from the last N days."""
    if not OBSERVATIONS_FILE.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    records = []
    with open(OBSERVATIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("ts", "") >= cutoff:
                    records.append(r)
            except Exception:
                pass
    return records


# ── Snapshot builder ──────────────────────────────────────────────────────────

def build_market_overview(days_back: int = 30) -> dict:
    """
    Compute the full market intelligence snapshot. Expensive — run once per pipeline cycle.
    Result is cached in market_snapshot.json.
    """
    outcomes     = _load_outcomes(days_back)
    observations = _load_observations(days_back)
    now          = datetime.now(timezone.utc)

    # ── Platform reliability ───────────────────────────────────────────────────
    platform_stats: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "successes": 0, "total_attempts_used": 0})
    for r in outcomes:
        plat = r.get("platform", "unknown")
        platform_stats[plat]["attempts"] += 1
        if r.get("success"):
            platform_stats[plat]["successes"] += 1
        platform_stats[plat]["total_attempts_used"] += r.get("attempts", 1)

    platform_reliability = {}
    for plat, stats in platform_stats.items():
        n = stats["attempts"]
        if n == 0:
            continue
        platform_reliability[plat] = {
            "success_rate":     round(stats["successes"] / n, 3),
            "avg_attempts":     round(stats["total_attempts_used"] / n, 2),
            "booking_count":    n,
            "reliability_score": round(stats["successes"] / n * (1 / max(stats["total_attempts_used"] / n, 1)), 3),
        }

    # ── Category + city success rates ─────────────────────────────────────────
    market_stats: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"attempts": 0, "successes": 0, "prices": [], "hours_list": []}
    ))
    for r in outcomes:
        cat  = r.get("category", "other")
        city = r.get("city", "unknown")
        market_stats[cat][city]["attempts"] += 1
        if r.get("success"):
            market_stats[cat][city]["successes"] += 1
        if r.get("price"):
            market_stats[cat][city]["prices"].append(float(r["price"]))
        if r.get("hours_before_start"):
            market_stats[cat][city]["hours_list"].append(float(r["hours_before_start"]))

    category_city_matrix = {}
    for cat, cities in market_stats.items():
        category_city_matrix[cat] = {}
        for city, stats in cities.items():
            n = stats["attempts"]
            if n == 0:
                continue
            prices     = stats["prices"]
            hours_list = stats["hours_list"]
            category_city_matrix[cat][city] = {
                "success_rate":  round(stats["successes"] / n, 3),
                "booking_count": n,
                "avg_price":     round(sum(prices) / len(prices), 2) if prices else None,
                "avg_hours_before_start": round(sum(hours_list) / len(hours_list), 1) if hours_list else None,
            }

    # ── Optimal booking windows ────────────────────────────────────────────────
    # Group successful bookings by hours_before_start bucket
    window_buckets: dict[str, dict] = {
        "0-4h":   {"count": 0, "successes": 0},
        "4-12h":  {"count": 0, "successes": 0},
        "12-24h": {"count": 0, "successes": 0},
        "24-48h": {"count": 0, "successes": 0},
        "48-72h": {"count": 0, "successes": 0},
    }
    for r in outcomes:
        h = r.get("hours_before_start", 0) or 0
        if h < 4:    bucket = "0-4h"
        elif h < 12: bucket = "4-12h"
        elif h < 24: bucket = "12-24h"
        elif h < 48: bucket = "24-48h"
        else:        bucket = "48-72h"
        window_buckets[bucket]["count"] += 1
        if r.get("success"):
            window_buckets[bucket]["successes"] += 1

    optimal_windows = {}
    for window, stats in window_buckets.items():
        n = stats["count"]
        optimal_windows[window] = {
            "booking_count": n,
            "success_rate":  round(stats["successes"] / n, 3) if n else 0,
        }

    best_window = max(optimal_windows.items(), key=lambda x: x[1]["success_rate"] if x[1]["booking_count"] >= 3 else 0)

    # ── Fill velocity (from observations) ─────────────────────────────────────
    # Slots that appear as "available" and then "booked" — time between = fill time
    booked_ids = {r["slot_id"] for r in observations if r.get("status") == "booked"}
    availability_start: dict[str, str] = {}
    fill_times: dict[str, list] = defaultdict(list)  # category -> [minutes]

    for r in observations:
        sid = r.get("slot_id", "")
        if not sid:
            continue
        if r.get("status") == "available" and sid not in availability_start:
            availability_start[sid] = r.get("ts", "")
        elif r.get("status") == "booked" and sid in availability_start:
            try:
                t0 = datetime.fromisoformat(availability_start[sid].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                minutes = (t1 - t0).total_seconds() / 60
                cat = r.get("category", "other")
                fill_times[cat].append(minutes)
            except Exception:
                pass

    fill_velocity = {}
    for cat, times in fill_times.items():
        if times:
            fill_velocity[cat] = {
                "avg_fill_minutes":    round(sum(times) / len(times), 1),
                "median_fill_minutes": round(sorted(times)[len(times) // 2], 1),
                "sample_count":        len(times),
            }

    # ── Current live inventory ─────────────────────────────────────────────────
    agg = ROOT / ".tmp" / "aggregated_slots.json"
    live_inventory: dict[str, dict] = {}
    if agg.exists():
        try:
            slots = json.loads(agg.read_text(encoding="utf-8"))
            inv: dict[str, dict] = defaultdict(lambda: {"count": 0, "cities": set(), "avg_price": [], "min_hours": 999})
            for s in slots:
                cat = s.get("category", "other")
                p   = float(s.get("our_price") or s.get("price") or 0)
                h   = s.get("hours_until_start") or 999
                c   = s.get("location_city", "")
                inv[cat]["count"] += 1
                if c:
                    inv[cat]["cities"].add(c)
                if p > 0:
                    inv[cat]["avg_price"].append(p)
                if h < inv[cat]["min_hours"]:
                    inv[cat]["min_hours"] = h

            for cat, data in inv.items():
                prices = data["avg_price"]
                live_inventory[cat] = {
                    "slot_count":  data["count"],
                    "city_count":  len(data["cities"]),
                    "avg_price":   round(sum(prices) / len(prices), 2) if prices else None,
                    "next_slot_hours": round(data["min_hours"], 1) if data["min_hours"] < 999 else None,
                }
        except Exception:
            pass

    snapshot = {
        "generated_at":           now.isoformat(),
        "data_window_days":       days_back,
        "total_booking_attempts": len(outcomes),
        "platform_reliability":   platform_reliability,
        "category_city_matrix":   category_city_matrix,
        "optimal_booking_windows": optimal_windows,
        "best_booking_window":    best_window[0] if best_window else None,
        "fill_velocity":          fill_velocity,
        "live_inventory":         live_inventory,
        "insights_summary": {
            "most_reliable_platform":   max(platform_reliability.items(), key=lambda x: x[1]["reliability_score"], default=(None, {}))[0],
            "highest_success_category": max(
                ((cat, max(cities.values(), key=lambda v: v["success_rate"]))
                 for cat, cities in category_city_matrix.items() if cities),
                key=lambda x: x[1]["success_rate"],
                default=(None, None)
            )[0] if category_city_matrix else None,
            "fastest_filling_category": min(fill_velocity.items(), key=lambda x: x[1]["avg_fill_minutes"], default=(None, {}))[0],
        },
    }

    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot


def get_market_snapshot(category: str = "", city: str = "") -> dict:
    """
    Return market insights, filtered to a specific category/city if requested.
    Uses cached snapshot if fresh (< 6 hours old).
    """
    snapshot = None
    if SNAPSHOT_FILE.exists():
        try:
            snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
            gen_at = datetime.fromisoformat(snapshot.get("generated_at", "1970-01-01").replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - gen_at).total_seconds() > 21600:
                snapshot = None  # stale, rebuild
        except Exception:
            snapshot = None

    if snapshot is None:
        snapshot = build_market_overview()

    if not category and not city:
        return snapshot

    # Filter to requested category/city
    filtered: dict = {
        "generated_at": snapshot["generated_at"],
        "category":     category or "all",
        "city":         city or "all",
        "optimal_booking_windows": snapshot.get("optimal_booking_windows", {}),
        "best_booking_window":     snapshot.get("best_booking_window"),
    }

    # Platform reliability (unchanged — global)
    filtered["platform_reliability"] = snapshot.get("platform_reliability", {})

    # Category/city filtered success rates
    matrix = snapshot.get("category_city_matrix", {})
    if category and city:
        city_lower = city.lower()
        cat_data = matrix.get(category, {})
        matched = {c: v for c, v in cat_data.items() if city_lower in c.lower()}
        filtered["success_rates"] = matched
    elif category:
        filtered["success_rates"] = matrix.get(category, {})
    else:
        filtered["success_rates"] = {cat: cities for cat, cities in matrix.items()
                                      if any(city.lower() in c.lower() for c in cities)}

    # Fill velocity filtered
    fv = snapshot.get("fill_velocity", {})
    filtered["fill_velocity"] = {category: fv[category]} if category and category in fv else fv

    # Live inventory filtered
    li = snapshot.get("live_inventory", {})
    filtered["live_inventory"] = {category: li[category]} if category and category in li else li

    # Demand signal: number of matching active intents
    sessions_file = ROOT / ".tmp" / "intent_sessions.json"
    demand_count = 0
    if sessions_file.exists():
        try:
            sessions = json.loads(sessions_file.read_text(encoding="utf-8"))
            for s in sessions.values():
                if s.get("status") != "monitoring":
                    continue
                c = s.get("constraints", {})
                if category and c.get("category") != category:
                    continue
                if city and city.lower() not in (c.get("city") or "").lower():
                    continue
                demand_count += 1
        except Exception:
            pass
    filtered["active_demand_signals"] = demand_count

    return filtered


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Market insights engine")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("refresh", help="Rebuild market_snapshot.json from raw event data")

    p_show = sub.add_parser("show", help="Print current market snapshot")
    p_show.add_argument("--category", default="")
    p_show.add_argument("--city",     default="")

    args = parser.parse_args()

    if args.cmd == "refresh":
        snap = build_market_overview()
        print(f"Snapshot rebuilt. {snap['total_booking_attempts']} booking records analysed.")
        print(f"Platforms tracked: {list(snap['platform_reliability'].keys())}")
        print(f"Best booking window: {snap['best_booking_window']}")
    elif args.cmd == "show":
        snap = get_market_snapshot(args.category, args.city)
        print(json.dumps(snap, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
