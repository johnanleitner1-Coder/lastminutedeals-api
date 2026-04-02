"""
generate_affiliate_links.py — Wrap platform booking URLs in affiliate tracking links.

Reads aggregated_slots.json, adds an affiliate_url field to each slot where a
tracking program exists, writes the file back in-place.

NOTE: booking_url is INTERNAL ONLY and must never be shown to end users.
      affiliate_url is used by the landing page for click-through tracking.
      For the integrated booking flow (our primary model), the user never
      sees either URL — complete_booking.py uses booking_url internally.

Affiliate programs supported:
    eventbrite      — aff param (Eventbrite Affiliate Program via Impact)
    ticketmaster    — wt.mc_id param (Ticketmaster Affiliate Program via CJ)
    seatgeek        — pid param (SeatGeek Affiliate Program via Impact)
    booking.com     — aid param (Booking.com Affiliate Partner Program)
    expedia         — affcid param (Expedia Partner Solutions)
    tripadvisor     — ta_affiliate_id param
    booksy          — referral param
    mindbody        — Verify program exists; stubbed here

Credentials in .env:
    EVENTBRITE_AFFILIATE_ID     (Impact affiliate ID — join at eventbrite.com/affiliate)
    TICKETMASTER_AFFILIATE_ID   (CJ affiliate param — join at partners.ticketmaster.com)
    SEATGEEK_AFFILIATE_ID       (Impact affiliate ID — join via SeatGeek partner portal)
    BOOKING_COM_AFFILIATE_ID
    EXPEDIA_AFFILIATE_ID
    TRIPADVISOR_AFFILIATE_ID
    BOOKSY_AFFILIATE_CODE

Usage:
    python tools/generate_affiliate_links.py [--data-file .tmp/aggregated_slots.json]
"""

import argparse
import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin

from dotenv import load_dotenv

load_dotenv()

DATA_FILE = Path(".tmp/aggregated_slots.json")

# ── Affiliate ID lookup from .env ─────────────────────────────────────────────
AFFILIATE_IDS = {
    "eventbrite":   os.getenv("EVENTBRITE_AFFILIATE_ID"),
    "ticketmaster": os.getenv("TICKETMASTER_AFFILIATE_ID"),
    "seatgeek":     os.getenv("SEATGEEK_AFFILIATE_ID"),
    "booking_com":  os.getenv("BOOKING_COM_AFFILIATE_ID"),
    "expedia":      os.getenv("EXPEDIA_AFFILIATE_ID"),
    "tripadvisor":  os.getenv("TRIPADVISOR_AFFILIATE_ID"),
    "booksy":       os.getenv("BOOKSY_AFFILIATE_CODE"),
}


def _add_param(url: str, key: str, value: str) -> str:
    """Append a query parameter to a URL."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{key}={value}"


def build_affiliate_url(platform: str, booking_url: str) -> str | None:
    """
    Return an affiliate-tracked URL for the given platform and booking URL.
    Returns None if no affiliate program is configured for this platform.
    """
    if not booking_url:
        return None

    platform = platform.lower()

    if platform == "eventbrite":
        aff_id = AFFILIATE_IDS.get("eventbrite")
        if not aff_id:
            return None
        # Eventbrite affiliate via Impact: append ?aff=<ID>
        return _add_param(booking_url, "aff", aff_id)

    elif platform == "ticketmaster":
        aff_id = AFFILIATE_IDS.get("ticketmaster")
        if not aff_id:
            return None
        # Ticketmaster affiliate via CJ: append wt.mc_id param
        return _add_param(booking_url, "wt.mc_id", aff_id)

    elif platform == "seatgeek":
        aff_id = AFFILIATE_IDS.get("seatgeek")
        if not aff_id:
            return None
        # SeatGeek affiliate via Impact: append pid param
        return _add_param(booking_url, "pid", aff_id)

    elif platform == "booking_com":
        aid = AFFILIATE_IDS.get("booking_com")
        if not aid:
            return None
        # Booking.com affiliate: append ?aid=<AID>
        return _add_param(booking_url, "aid", aid)

    elif platform in ("airbnb",):
        # Airbnb discontinued its affiliate program in 2021
        return None

    elif platform == "expedia":
        aff_id = AFFILIATE_IDS.get("expedia")
        if not aff_id:
            return None
        return _add_param(booking_url, "affcid", aff_id)

    elif platform == "tripadvisor":
        ta_id = AFFILIATE_IDS.get("tripadvisor")
        if not ta_id:
            return None
        return _add_param(booking_url, "ta_affiliate_id", ta_id)

    elif platform == "booksy":
        code = AFFILIATE_IDS.get("booksy")
        if not code:
            return None
        return _add_param(booking_url, "referral", code)

    elif platform == "mindbody":
        # Mindbody referral program — verify URL format when you get your code
        # Stubbed: return None until confirmed program details
        return None

    # Platform has no affiliate program
    return None


def main():
    parser = argparse.ArgumentParser(description="Generate affiliate tracking links")
    parser.add_argument("--data-file", default=str(DATA_FILE))
    args = parser.parse_args()

    data_path = Path(args.data_file)
    if not data_path.exists():
        print(f"ERROR: {data_path} not found. Run aggregate_slots.py first.")
        sys.exit(1)

    slots = json.loads(data_path.read_text(encoding="utf-8"))

    # Warn if no affiliate IDs are configured
    configured = [k for k, v in AFFILIATE_IDS.items() if v]
    if not configured:
        print("WARN: No affiliate IDs found in .env. affiliate_url will be null for all slots.")
        print("      Set BOOKING_COM_AFFILIATE_ID, EXPEDIA_AFFILIATE_ID, etc. to enable tracking.")

    linked = 0
    skipped = 0

    for slot in slots:
        platform    = slot.get("platform", "")
        booking_url = slot.get("booking_url", "")
        aff_url     = build_affiliate_url(platform, booking_url)

        slot["affiliate_url"] = aff_url
        if aff_url:
            linked += 1
        else:
            skipped += 1

    data_path.write_text(json.dumps(slots, indent=2, default=str), encoding="utf-8")

    print(f"Affiliate links: {linked} generated, {skipped} skipped (no program or no ID)")
    print(f"Output → {data_path} (updated in-place)")


if __name__ == "__main__":
    main()
