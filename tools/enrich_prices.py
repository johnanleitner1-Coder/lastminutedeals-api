"""
enrich_prices.py — Fill in missing prices for slots where the API didn't return one.

Runs AFTER aggregate_slots.py, BEFORE compute_pricing.py.

Strategy per platform:
  eventbrite  — Fetch the individual event page; extract price from JSON-LD <offers>
  meetup      — All Meetup events without a price are free (price = 0)
  ticketmaster— Page is 401-blocked; leave price as None (filtered from landing page)
  seatgeek    — Always returns prices from API; nothing to do
  others      — Leave unchanged

Price Cache (.tmp/price_cache.json):
  Maps slot_id → price. Built up across runs so we never re-fetch a page we've
  already enriched. This is critical: aggregate_slots.py rebuilds from scratch
  each run and loses enriched prices; the cache restores them instantly.

Usage:
    python tools/enrich_prices.py [--data-file .tmp/aggregated_slots.json]
                                  [--max-eventbrite 500]
                                  [--delay 0.6]
"""

import argparse
import json
import re
import sys
import time
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DATA_FILE   = Path(".tmp/aggregated_slots.json")
CACHE_FILE  = Path(".tmp/price_cache.json")   # persists across runs

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def load_price_cache() -> dict:
    """Load {slot_id: price} from cache file. Returns empty dict if missing."""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_price_cache(cache: dict) -> None:
    """Persist price cache to disk. Keeps last 50k entries."""
    if len(cache) > 50_000:
        # Trim oldest entries (dict insertion order in Python 3.7+)
        keys = list(cache.keys())
        cache = {k: cache[k] for k in keys[-50_000:]}
    CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def extract_eventbrite_price(html: str) -> float | None:
    """
    Extract price from Eventbrite event page HTML.

    Tries three extraction paths in order:
    1. JSON-LD <script type="application/ld+json"> offers array
    2. data-spec="eds-text" price patterns in page HTML
    3. Fallback pattern matching "$X.XX" near "ticket" text
    """
    # ── Path 1: JSON-LD structured data ──────────────────────────────────────
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for block in ld_blocks:
        try:
            obj = json.loads(block.strip())
            # obj might be a list or a dict
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                offers = item.get("offers") or []
                if isinstance(offers, dict):
                    offers = [offers]
                for offer in offers:
                    price = offer.get("price") or offer.get("lowPrice")
                    if price is not None:
                        try:
                            return float(price)
                        except (TypeError, ValueError):
                            pass
        except (json.JSONDecodeError, AttributeError):
            continue

    # ── Path 2: __SERVER_DATA__ JSON blob ────────────────────────────────────
    # Use brace-counting to find the exact extent of the JSON object rather than
    # regex .*? which truncates on the first closing brace of a nested object.
    sd_start = html.find("window.__SERVER_DATA__ =")
    if sd_start != -1:
        obj_start = html.find("{", sd_start)
        if obj_start != -1:
            depth = 0
            obj_end = obj_start
            for i in range(obj_start, min(obj_start + 500_000, len(html))):
                if html[i] == "{":
                    depth += 1
                elif html[i] == "}":
                    depth -= 1
                    if depth == 0:
                        obj_end = i + 1
                        break
            if obj_end > obj_start:
                try:
                    data = json.loads(html[obj_start:obj_end])
                    ticket_avail = (
                        data.get("event", {})
                            .get("ticket_availability", {})
                    )
                    min_price = ticket_avail.get("minimum_ticket_price", {})
                    if min_price:
                        major = min_price.get("major_value")
                        if major is not None:
                            try:
                                return float(major)
                            except (TypeError, ValueError):
                                pass
                except (json.JSONDecodeError, AttributeError, KeyError):
                    pass

    # ── Path 3: Regex price pattern in raw HTML ───────────────────────────────
    # Look for patterns like "Starting at $12.50" or "$12.50 - $45.00"
    price_match = re.search(
        r'(?:Starting\s+at\s+|From\s+|Tickets?\s+from\s+)?\$\s*([\d,]+(?:\.\d{1,2})?)',
        html,
        re.IGNORECASE,
    )
    if price_match:
        try:
            return float(price_match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            pass

    return None  # could not determine price


def enrich_eventbrite(
    slots: list[dict],
    session: requests.Session,
    max_fetch: int,
    delay: float,
    price_cache: dict,
) -> tuple[int, int, int]:
    """
    For each Eventbrite slot with price=None:
      1. Check price_cache — restore instantly if found.
      2. Fetch the event page for remaining slots.

    Returns (from_cache, from_fetch, failed).
    """
    UNKNOWN_SENTINEL = "__unknown__"

    # Separate: need enrichment
    to_enrich = [
        s for s in slots
        if s.get("platform") == "eventbrite" and s.get("price") is None
    ]

    # Step 1: apply cache
    from_cache = 0
    still_missing = []
    for slot in to_enrich:
        sid = slot.get("slot_id", "")
        cached = price_cache.get(sid)
        if cached is not None:
            if cached != UNKNOWN_SENTINEL:
                slot["price"] = cached
                from_cache += 1
            # If UNKNOWN_SENTINEL, skip fetching again
        else:
            still_missing.append(slot)

    print(f"  Eventbrite: {len(to_enrich)} need prices | {from_cache} from cache | {len(still_missing)} to fetch (cap={max_fetch})")

    # Step 2: fetch pages for the rest
    from_fetch = 0
    failed = 0

    for slot in still_missing[:max_fetch]:
        url = slot.get("booking_url") or slot.get("affiliate_url")
        sid = slot.get("slot_id", "")
        if not url:
            price_cache[sid] = UNKNOWN_SENTINEL
            failed += 1
            continue

        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                price = extract_eventbrite_price(resp.text)
                if price is not None:
                    slot["price"] = price
                    price_cache[sid] = price
                    from_fetch += 1
                else:
                    price_cache[sid] = UNKNOWN_SENTINEL
                    failed += 1
            elif resp.status_code == 404:
                price_cache[sid] = UNKNOWN_SENTINEL
                failed += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1

        time.sleep(delay)

    return from_cache, from_fetch, failed


def enrich_meetup(slots: list[dict]) -> int:
    """
    Meetup events without a price are free.
    Returns count of slots updated.
    """
    updated = 0
    for slot in slots:
        if slot.get("platform") == "meetup" and slot.get("price") is None:
            slot["price"] = 0.0
            updated += 1
    return updated


def main():
    parser = argparse.ArgumentParser(description="Enrich missing prices in aggregated slots")
    parser.add_argument("--data-file", default=str(DATA_FILE))
    parser.add_argument(
        "--max-eventbrite",
        type=int,
        default=500,
        help="Max NEW Eventbrite event pages to fetch per run (cached results are free)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.6,
        help="Seconds to wait between Eventbrite page fetches",
    )
    args = parser.parse_args()

    data_path = Path(args.data_file)
    if not data_path.exists():
        print(f"ERROR: {data_path} not found. Run aggregate_slots.py first.")
        sys.exit(1)

    slots = json.loads(data_path.read_text(encoding="utf-8"))
    total = len(slots)

    # Baseline stats
    has_price = sum(1 for s in slots if s.get("price") is not None)
    no_price  = total - has_price
    print(f"Loaded {total} slots: {has_price} have prices, {no_price} need enrichment")

    # ── Load price cache ──────────────────────────────────────────────────────
    price_cache = load_price_cache()
    print(f"Price cache: {len(price_cache)} entries loaded")

    # ── Meetup: mark free ─────────────────────────────────────────────────────
    meetup_updated = enrich_meetup(slots)
    print(f"Meetup: marked {meetup_updated} events as free ($0)")

    # ── Eventbrite: cache + fetch individual pages ────────────────────────────
    session = make_session()
    eb_cached, eb_fetched, eb_failed = enrich_eventbrite(
        slots, session, args.max_eventbrite, args.delay, price_cache
    )
    print(f"Eventbrite: {eb_cached} from cache, {eb_fetched} newly fetched, {eb_failed} failed")

    # ── Persist cache ─────────────────────────────────────────────────────────
    save_price_cache(price_cache)
    print(f"Price cache: {len(price_cache)} entries saved")

    # Final stats
    has_price_after = sum(1 for s in slots if s.get("price") is not None)
    improvement = has_price_after - has_price
    print(f"\nEnrichment complete: {has_price} -> {has_price_after} slots with prices (+{improvement})")
    pct = has_price_after / total * 100 if total else 0
    print(f"Coverage: {has_price_after}/{total} = {pct:.1f}%")

    # Write back
    data_path.write_text(json.dumps(slots, indent=2, default=str), encoding="utf-8")
    print(f"Updated: {data_path}")


if __name__ == "__main__":
    main()
