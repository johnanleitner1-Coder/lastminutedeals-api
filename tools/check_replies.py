"""
check_replies.py — Fetch and display inbound email replies stored in Supabase.

Usage:
    python tools/check_replies.py [--limit 50]
"""

import argparse
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("ERROR: requests not installed.")
    sys.exit(1)

API_BASE = os.getenv("BOOKING_API_URL", "https://web-production-dc74b.up.railway.app")
API_KEY  = os.getenv("LMD_WEBSITE_API_KEY", "")


def fetch_replies(limit: int = 50) -> list[dict]:
    resp = requests.get(
        f"{API_BASE}/api/inbound-email/list",
        headers={"X-Api-Key": API_KEY},
        params={"limit": limit},
        timeout=15,
    )
    if resp.status_code == 401:
        print("ERROR: Unauthorized — check LMD_WEBSITE_API_KEY in .env")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json().get("emails", [])


def main():
    parser = argparse.ArgumentParser(description="Check inbound email replies")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    print(f"Fetching up to {args.limit} inbound emails...\n")
    emails = fetch_replies(args.limit)

    if not emails:
        print("No replies received yet.")
        return

    print(f"{len(emails)} reply(s) found:\n")
    for i, e in enumerate(emails, 1):
        print(f"{'='*60}")
        print(f"[{i}] {e.get('received_at', '')[:19]}")
        print(f"FROM:    {e.get('from', '')}")
        print(f"SUBJECT: {e.get('subject', '')}")
        print(f"{'─'*60}")
        body = (e.get('body') or '').strip()
        print(body[:1000])
        if len(body) > 1000:
            print(f"... [{len(body) - 1000} more chars]")
        print()


if __name__ == "__main__":
    main()
