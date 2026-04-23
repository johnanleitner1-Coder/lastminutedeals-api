"""
send_platform_outreach.py — Email distribution platforms to pitch as a supply partner.

Unlike supplier outreach (inviting Bokun operators to connect), this emails
OTAs and aggregator-of-aggregator platforms to offer our OCTO inventory as
a supply source for their buyer traffic.

Usage:
    python tools/send_platform_outreach.py [--dry-run]
"""

import argparse
import json
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
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "").strip()
EMAIL_FROM       = os.getenv("EMAIL_FROM", "bookings@lastminutedealshq.com")
EMAIL_FROM_NAME  = "John Anleitner"
REPLY_TO         = "johnanleitner1@gmail.com"

TARGETS = [
    {
        "name": "Bridgify",
        "to": "contact@bridgify.io",
        "subject": "OCTO Supply Partner Inquiry — 29 Suppliers, 8,000+ Live Slots",
        "body": """\
Hi Bridgify team,

I'm John, founder of Last Minute Deals HQ. We operate an OCTO-native \
booking pipeline connected to 29 tour and activity suppliers across 16 \
countries — Iceland, Egypt, Japan, Italy, Portugal, Turkey, Morocco, \
Tanzania, and more. Our inventory includes glacier hikes, e-bike tours, \
cooking classes, Nile cruises, desert safaris, and photography experiences. \
~8,000 live bookable slots, refreshed every 4 hours.

We'd like to explore becoming a supply source for Bridgify. Our inventory \
is real-time via the OCTO open standard, which I understand you already \
support. If we carry products your network doesn't already have, \
integration should be straightforward.

A few details:
- 29 OCTO-connected suppliers (Bokun platform)
- 8,000+ live slots across 30 cities
- Real-time availability and booking via OCTO API
- Focus on last-minute / short-lead inventory (<=72h)
- Stripe-integrated checkout with auth-hold

Happy to share our API docs or jump on a call. What's the best way to \
evaluate catalog overlap?

John Anleitner
President, Last Minute Deals HQ LLC
https://api.lastminutedealshq.com
""",
    },
    {
        "name": "Holibob",
        "to": "Suppliersupport@holibob.tech",
        "subject": "Supply Partner Inquiry — 29 OCTO Suppliers, 16 Countries",
        "body": """\
Hi Holibob team,

I'm John, founder of Last Minute Deals HQ. We run an OCTO-native booking \
pipeline with 29 tour and activity suppliers across 16 countries — \
including destinations like Reykjavik, Rome, Cairo, Kyoto, Lisbon, \
Marrakech, and Dar es Salaam. ~8,000 live bookable slots covering glacier \
hikes, e-bike tours, cooking classes, Nile cruises, desert tours, and more.

We're interested in becoming a supply partner for Holibob. Our inventory \
is served via the OCTO open standard with real-time availability and \
booking. If we carry experiences your network doesn't already have, we'd \
love to explore integration.

Quick overview:
- 29 suppliers on Bokun/OCTO
- 8,000+ live slots, 30 cities, 16 countries
- Real-time availability, refreshed every 4 hours
- Specialization in last-minute / short-lead availability
- REST API + MCP server for AI agent distribution

Happy to share API docs or schedule a call to discuss catalog overlap.

John Anleitner
President, Last Minute Deals HQ LLC
https://api.lastminutedealshq.com
""",
    },
    {
        "name": "Klook",
        "to": "nathan.szabo@klook.com",
        "subject": "Connectivity Partner Inquiry — 29 Suppliers, 8,000+ Slots, 16 Countries",
        "body": """\
Hi Nathan,

I'm John, founder of Last Minute Deals HQ. We operate a booking pipeline \
connected to 29 tour and activity suppliers across 16 countries — Iceland, \
Egypt, Japan, Italy, Portugal, Turkey, Morocco, Tanzania, and more. ~8,000 \
live bookable slots covering glacier hikes, e-bike tours, cooking classes, \
Nile cruises, desert safaris, and photography experiences.

We'd like to explore becoming a connectivity partner for Klook. Our \
inventory is API-accessible with real-time availability and booking. We \
specialize in last-minute / short-lead availability (<=72h) — a segment \
that's hard for traditional supply partners to fill consistently.

Quick overview:
- 29 connected suppliers (Bokun/OCTO platform)
- 8,000+ live slots across 30 cities
- Real-time availability, refreshed every 4 hours
- REST API with search, booking, and status endpoints
- Categories: tours, activities, wellness, photography, hospitality

Happy to share API docs or discuss how our inventory could complement \
Klook's existing supply. Is your team the right contact for connectivity \
partner conversations?

John Anleitner
President, Last Minute Deals HQ LLC
https://api.lastminutedealshq.com
""",
    },
    {
        "name": "Headout",
        "to": "support@headout.com",
        "subject": "Supply Partner Inquiry — 29 Suppliers, 16 Countries via OCTO",
        "body": """\
Hi Headout team,

I'm John, founder of Last Minute Deals HQ. We operate an OCTO-native \
booking pipeline connected to 29 tour and activity suppliers across 16 \
countries and 30 cities — including Rome, Reykjavik, Cairo, Kyoto, Lisbon, \
Marrakech, Puerto Vallarta, and more. ~8,000 live bookable slots covering \
e-bike tours, glacier hikes, cooking classes, desert safaris, Nile \
cruises, photography experiences, and more.

We'd like to explore listing our supplier inventory on Headout as a \
supply/connectivity partner. Our inventory is API-accessible with \
real-time availability via the OCTO open standard, and we can map to \
your API format.

Quick overview:
- 29 OCTO-connected suppliers (Bokun platform)
- 8,000+ live slots across 30 cities
- Real-time availability, refreshed every 4 hours
- Focus on last-minute / short-lead inventory
- Categories: tours, activities, wellness, photography, hospitality

I saw the Experience Providers Hub — happy to register there if that's \
the right path, or connect with your supply partnerships team directly. \
What would you recommend for a multi-supplier connectivity partner?

John Anleitner
President, Last Minute Deals HQ LLC
https://api.lastminutedealshq.com
""",
    },
]

SENT_LOG = Path(__file__).parent / "seeds" / "platform_outreach_sent.json"


def send_email(to: str, subject: str, body: str) -> bool:
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from":             {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "reply_to":         {"email": REPLY_TO, "name": EMAIL_FROM_NAME},
        "subject":          subject,
        "content":          [{"type": "text/plain", "value": body}],
    }
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            data=json.dumps(payload),
            timeout=15,
        )
        return resp.status_code in (200, 202)
    except Exception as e:
        print(f"  ERROR sending to {to}: {e}")
        return False


def load_sent() -> set:
    if SENT_LOG.exists():
        return set(json.loads(SENT_LOG.read_text(encoding="utf-8")))
    return set()


def save_sent(sent: set):
    SENT_LOG.write_text(json.dumps(sorted(sent), indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        sys.exit(1)

    sent = load_sent()
    targets = [t for t in TARGETS if t["to"] not in sent]

    if not targets:
        print("No new targets to send to.")
        return

    print(f"{len(targets)} platform(s) to contact (skipping {len(sent)} already sent)")

    newly_sent = set()
    for t in targets:
        print(f"\n{'='*60}")
        print(f"TO:      {t['to']}")
        print(f"SUBJECT: {t['subject']}")
        print(f"{'~'*60}")
        print(t["body"])

        if args.dry_run:
            print("[DRY RUN] Not sent.")
            continue

        ok = send_email(t["to"], t["subject"], t["body"])
        print(f"[{'SENT' if ok else 'FAILED'}] {t['to']}")
        if ok:
            newly_sent.add(t["to"])

    if not args.dry_run:
        save_sent(sent | newly_sent)

    print(f"\n{'='*60}")
    if not args.dry_run:
        print(f"Platform outreach complete. {len(newly_sent)}/{len(targets)} emails sent.")


if __name__ == "__main__":
    main()
