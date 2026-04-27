"""
send_supplier_followups.py — Reply to inbound supplier leads.

These suppliers reached out to us — respond warmly and get them connected.

Usage:
    python tools/send_supplier_followups.py [--dry-run]
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

SENT_FILE = Path(__file__).parent / "seeds" / "supplier_followup_sent.json"

TARGETS = [
    {
        "name": "Zubia",
        "company": "European Voyages",
        "to": "info@european-voyages.com",
        "subject": "Re: Partnership — European Voyages x Last Minute Deals",
        "body": """\
Hi Zubia,

Thanks for reaching out! We'd love to connect European Voyages to our \
network.

We're a Bokun reseller that distributes tours through AI channels — our \
ChatGPT integration, MCP server for AI agents, and direct API. We're \
currently connected to 40 operators across 100+ cities and handle all \
payment processing via Stripe with instant confirmation.

Getting connected is straightforward:
1. We'll send you a contract proposal through the Bokun marketplace
2. You accept it in your Bokun dashboard
3. Your tours automatically appear in our inventory within 4 hours

Our standard terms are commission-based — we take a percentage on each \
booking and handle all customer-facing payment and support. No upfront \
costs, no monthly fees.

I'll send the Bokun contract proposal today. Once you accept, your \
Paris walking tours, food tours, and city experiences will be live \
across all our channels.

Looking forward to working together!

John Anleitner
President, Last Minute Deals HQ LLC
https://lastminutedealshq.com
""",
    },
    {
        "name": "Zoe",
        "company": "Experience Galway",
        "to": "info@experiencegalway.ie",
        "subject": "Re: Partnership — Experience Galway x Last Minute Deals",
        "body": """\
Hi Zoe,

Great to hear from you! We'd love to add Galway experiences to our network.

We're a Bokun-based reseller distributing tours through AI-powered \
channels — a ChatGPT integration in the GPT Store, an MCP server for \
AI agents, and a direct booking API. Currently connected to 40 operators \
across 100+ cities worldwide.

If you're on Bokun, connecting is simple:
1. We send a contract proposal through the Bokun marketplace
2. You accept in your dashboard
3. Your experiences go live in our inventory automatically

Commission-based, no upfront costs. We handle payment processing \
(Stripe) and customer communication.

Are you currently on Bokun? If so, I'll send the contract proposal \
right away. If you're on a different booking system, let me know and \
we can figure out the best way to connect.

Galway is a fantastic destination — walking tours, food experiences, \
Cliffs of Moher day trips, Aran Islands... your inventory would be a \
great fit.

Looking forward to it!

John Anleitner
President, Last Minute Deals HQ LLC
https://lastminutedealshq.com
""",
    },
    {
        "name": "Mark",
        "company": "Stoke Travel",
        "to": "info@stoketravel.com",
        "subject": "Following up — Stoke Travel x Last Minute Deals",
        "body": """\
Hi Mark,

Following up on the partnership note I sent a couple weeks ago. We're a \
Bokun-based reseller distributing tours through AI channels — ChatGPT \
integration, MCP server for AI agents, and direct booking API.

Since then we've grown to 40 connected operators across 100+ cities. \
Stoke Travel's adventure experiences in Barcelona would be a great \
addition.

If you're interested, the connection is simple — we send a contract \
proposal through the Bokun marketplace, you accept, and your tours go \
live automatically. Commission-based, no upfront costs.

Happy to answer any questions or jump on a quick call if that's easier.

John Anleitner
President, Last Minute Deals HQ LLC
https://lastminutedealshq.com
""",
    },
]


def _load_sent():
    if SENT_FILE.exists():
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    return []


def _save_sent(sent):
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(json.dumps(sent, indent=2), encoding="utf-8")


def _send_email(target, dry_run=False):
    to_addr = target["to"]
    name = target["name"]
    company = target["company"]

    sent = _load_sent()
    if to_addr in sent:
        print(f"  SKIP {name} ({company}) — already sent")
        return False

    payload = {
        "personalizations": [{"to": [{"email": to_addr, "name": name}]}],
        "from": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "reply_to": {"email": REPLY_TO, "name": EMAIL_FROM_NAME},
        "subject": target["subject"],
        "content": [{"type": "text/plain", "value": target["body"]}],
    }

    if dry_run:
        print(f"  DRY RUN → {name} <{to_addr}> ({company})")
        print(f"    Subject: {target['subject']}")
        return True

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if resp.status_code in (200, 201, 202):
        print(f"  SENT → {name} <{to_addr}> ({company})")
        sent.append(to_addr)
        _save_sent(sent)
        return True
    else:
        print(f"  FAIL → {name} <{to_addr}> — {resp.status_code}: {resp.text}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set")
        sys.exit(1)

    print(f"Supplier follow-ups — {len(TARGETS)} targets")
    print(f"{'DRY RUN' if args.dry_run else 'LIVE SEND'}\n")

    sent_count = 0
    for target in TARGETS:
        if _send_email(target, dry_run=args.dry_run):
            sent_count += 1

    print(f"\nDone. {sent_count}/{len(TARGETS)} emails {'would be sent' if args.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
