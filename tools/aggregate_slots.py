"""
aggregate_slots.py — Read all platform slot files from .tmp/, deduplicate on slot_id,
filter to slots within the time window, and write a single aggregated output file.

Runs after all fetch_*_slots.py tools have completed.

Usage:
    python tools/aggregate_slots.py [--hours-ahead 72] [--tmp-dir .tmp]

Output:
    .tmp/aggregated_slots.json   — deduplicated, filtered, sorted by hours_until_start
"""

import argparse
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from datetime import timezone, datetime

sys.path.insert(0, str(Path(__file__).parent))
from normalize_slot import compute_hours_until, is_within_window

TMP_DIR    = Path(".tmp")
OUTPUT_FILE = TMP_DIR / "aggregated_slots.json"

# Files to read — in priority order (first seen wins on duplicate slot_id)
# Confidence ranking: api > ical > scrape
PLATFORM_FILES = [
    "octo_slots.json",           # OCTO standard: Bokun, Ventrata, Peek Pro, Xola, Zaui
]


def load_platform_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print(f"  WARN: {path.name} is not a list — skipping")
            return []
        return data
    except json.JSONDecodeError as e:
        print(f"  WARN: Could not parse {path.name}: {e}")
        return []


def refresh_hours_until(slot: dict) -> dict:
    """Recompute hours_until_start at aggregation time (fetch may have been minutes ago)."""
    slot["hours_until_start"] = compute_hours_until(slot.get("start_time", ""))
    return slot


def main():
    parser = argparse.ArgumentParser(description="Aggregate and deduplicate platform slot files")
    parser.add_argument("--hours-ahead", type=float, default=168.0,
                        help="Only include slots within this many hours (default: 168 = 1 week)")
    parser.add_argument("--tmp-dir", default=str(TMP_DIR),
                        help="Directory containing *_slots.json files")
    args = parser.parse_args()

    tmp_dir = Path(args.tmp_dir)
    if not tmp_dir.exists():
        print(f"ERROR: tmp-dir '{tmp_dir}' does not exist. Run a fetch_* tool first.")
        sys.exit(1)

    # ── Load all platform files ───────────────────────────────────────────────
    seen_ids:  dict[str, dict] = {}   # slot_id → slot (first-seen wins)
    stats = {
        "files_read":     0,
        "raw_total":      0,
        "no_slot_id":     0,
        "duplicates":     0,
        "out_of_window":  0,
        "in_past":        0,
        "kept":           0,
    }

    # Process known files first (priority order), then any extra *_slots.json.
    # Explicitly exclude the output file to prevent circular re-ingestion.
    ordered = [tmp_dir / f for f in PLATFORM_FILES if (tmp_dir / f).exists()]
    extras  = [
        p for p in tmp_dir.glob("*_slots.json")
        if p not in ordered and p.name != OUTPUT_FILE.name
    ]
    all_files = ordered + sorted(extras)

    for path in all_files:
        slots = load_platform_file(path)
        stats["files_read"] += 1
        stats["raw_total"]  += len(slots)
        print(f"  {path.name}: {len(slots)} records")

        for slot in slots:
            slot_id = slot.get("slot_id")
            if not slot_id:
                stats["no_slot_id"] += 1
                continue

            # Refresh timing so we're computing from aggregation time
            slot = refresh_hours_until(slot)
            hours = slot.get("hours_until_start")

            if hours is None:
                stats["out_of_window"] += 1
                continue

            if hours < 0:
                stats["in_past"] += 1
                continue

            if hours > args.hours_ahead:
                stats["out_of_window"] += 1
                continue

            if slot_id in seen_ids:
                stats["duplicates"] += 1
                continue

            seen_ids[slot_id] = slot
            stats["kept"] += 1

    # ── Sort by urgency (soonest first) ──────────────────────────────────────
    aggregated = sorted(
        seen_ids.values(),
        key=lambda s: s.get("hours_until_start") or 9999,
    )

    # ── Write output ─────────────────────────────────────────────────────────
    tmp_dir.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(aggregated, indent=2, default=str), encoding="utf-8")

    # ── Print summary ─────────────────────────────────────────────────────────
    print(f"\n" + "-"*50)
    print(f"Aggregation complete")
    print(f"  Files read      : {stats['files_read']}")
    print(f"  Raw records     : {stats['raw_total']}")
    print(f"  No slot_id      : {stats['no_slot_id']}")
    print(f"  Duplicates      : {stats['duplicates']}")
    print(f"  Out of window   : {stats['out_of_window']}")
    print(f"  In the past     : {stats['in_past']}")
    print(f"  Kept (output)   : {stats['kept']}")
    print(f"  Output          : {OUTPUT_FILE}")

    # Category breakdown
    from collections import Counter
    cats = Counter(s.get("category", "unknown") for s in aggregated)
    if cats:
        print(f"\n  By category:")
        for cat, count in cats.most_common():
            print(f"    {cat:<25} {count}")

    print("-"*50)
    return stats["kept"]


if __name__ == "__main__":
    kept = main()
    sys.exit(0 if kept >= 0 else 1)
