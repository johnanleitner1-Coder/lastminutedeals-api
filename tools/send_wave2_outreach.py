"""
send_wave2_outreach.py — Wave 2: AI newsletters, GPT directories, travel tech press.

These are higher-reach targets than wave 1 bloggers.
Focus: newsletter curators with millions of readers, GPT directories, press.

Usage:
    python tools/send_wave2_outreach.py [--dry-run]
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

SENT_FILE = Path(__file__).parent / "seeds" / "wave2_outreach_sent.json"

TARGETS = [
    # ── AI Newsletters (massive audiences) ────────────────────────────────
    {
        "name": "Rowan Cheung",
        "outlet": "The Rundown AI (2M+ subscribers)",
        "to": "rowancheung@gmail.com",
        "subject": "New MCP tool: AI agents can now book real tours (1 of only 3 in existence)",
        "body": """\
Hi Rowan,

Quick pitch for The Rundown — this is the kind of "AI tool that \
actually does something new" story your readers love.

We built one of only 3 MCP servers in the world that can book real \
tours and activities. 40 local operators across 100+ cities — Iceland \
glacier hikes, Rome cooking classes, Istanbul Bosphorus cruises, Egypt \
desert safaris. Real Stripe payments, instant confirmation with the \
operator.

What makes it interesting for your audience:
- Works as an MCP server (any AI agent can search + book tours)
- Free ChatGPT Custom GPT in the GPT Store ("Last Minute Tour Finder")
- REST API with free API keys for developers
- One of only 3 bookable tour APIs in the MCP ecosystem (alongside \
TourRadar and Peek)
- Focus on last-minute: tours departing in the next 72 hours

This is a real example of AI agents completing real-world transactions, \
not just planning or recommending.

Live API: https://api.lastminutedealshq.com/slots?city=Rome
Docs: https://lastminutedealshq.com/openapi.json

Happy to provide any additional details.

John Anleitner
Last Minute Deals HQ
""",
    },
    # ── Travel Tech Press ─────────────────────────────────────────────────
    {
        "name": "Skift Editorial",
        "outlet": "Skift",
        "to": "tips@skift.com",
        "subject": "Story tip: First AI agents booking real tours via MCP protocol",
        "body": """\
Hi Skift team,

Story tip: AI agents are now booking real tours — not just planning \
itineraries.

Last Minute Deals HQ operates one of only 3 MCP servers worldwide \
that can execute real tour bookings. We connect 40 local tour operators \
across 100+ cities via the OCTO open standard (Bokun platform), with \
inventory refreshing every 4 hours.

The interesting angle: while AI travel planners like Layla and Kayak \
focus on flights and hotels, nobody has solved activity booking for AI \
agents. When someone asks ChatGPT "book me a walking tour in Rome \
tomorrow," there are now exactly 3 systems that can actually do it. \
We're one of them.

Technical details:
- MCP-over-HTTP protocol at api.lastminutedealshq.com/mcp
- Also available as a ChatGPT Custom GPT in the GPT Store
- Real Stripe checkout + OCTO booking confirmation
- 40 suppliers across Iceland, Italy, Turkey, Egypt, Japan, Portugal, \
Morocco, Brazil, and 40+ more countries
- Commission-based revenue model, no upfront costs to suppliers

This sits at the intersection of two trends you cover: AI agents moving \
from planning to transacting, and tours & activities catching up to \
flights & hotels in tech adoption.

Happy to provide data, demo access, or founder interview.

John Anleitner
President, Last Minute Deals HQ LLC
""",
    },
    # ── GPT Directories ───────────────────────────────────────────────────
    {
        "name": "Dave Hu",
        "outlet": "Featured GPTs",
        "to": "contact@featuredgpts.com",
        "subject": "Travel GPT submission: the only one that actually books tours",
        "body": """\
Hi Dave,

I noticed your travel GPT category has Kayak, AllTrails, AirTrack, \
Nomad List, and Marcos. Great list — but none of them can actually \
book a tour or activity.

We built Last Minute Tour Finder, a free ChatGPT GPT that searches \
live inventory from 40 tour operators across 100+ cities and gives \
users a direct booking link. Glacier hikes in Iceland, food tours in \
Istanbul, cooking classes in Rome — with real Stripe checkout and \
instant confirmation.

It's the only travel GPT I'm aware of that completes the transaction, \
not just the planning.

I'll also submit through your form, but wanted to reach out directly \
since this fills a clear gap in your travel category.

GPT Store name: Last Minute Tour Finder
Category: Travel and Tourism

John Anleitner
Last Minute Deals HQ
""",
    },
    # ── Tech Publications ─────────────────────────────────────────────────
    {
        "name": "MakeUseOf Editorial",
        "outlet": "MakeUseOf",
        "to": "editors@makeuseof.com",
        "subject": "For your travel AI coverage: a ChatGPT app that actually books",
        "body": """\
Hi MakeUseOf team,

Your recent article on free travel planning AI apps noted that none \
of the 7 tools reviewed can actually book anything — users should \
"only treat it as a template or guide."

We built the tool that closes that gap. Last Minute Tour Finder is a \
free Custom GPT in the ChatGPT Store connected to live inventory from \
40 tour operators across 100+ cities. Users search by city, see real \
departures in the next 72 hours, and book directly via Stripe checkout \
with instant confirmation.

Unlike Layla, Wonderplan, or Curiosio (all in your review), this one \
actually completes the transaction.

It would make a strong addition to your "7 Free Travel Planning AI \
Apps" article or a standalone piece about AI moving from planning to \
booking.

Search "Last Minute Tour Finder" in the GPT Store to try it. No \
account or API key needed.

John Anleitner
Last Minute Deals HQ
https://lastminutedealshq.com
""",
    },
    # ── AI Agent / MCP Developer Community ────────────────────────────────
    {
        "name": "Shayla Martin",
        "outlet": "AFAR Magazine (editor)",
        "to": "smartin@afar.com",
        "subject": "AI travel update: a ChatGPT tool that actually books tours now",
        "body": """\
Hi Shayla,

AFAR tested AI travel planning apps last year and found that most stop \
short of actually helping you book. There's now a tool that goes all \
the way.

Last Minute Tour Finder is a free ChatGPT Custom GPT connected to 37 \
local tour operators across 100+ cities. Users can search for tours \
departing in the next few days and book directly — real Stripe checkout, \
instant confirmation with the operator.

The "last minute" angle is the interesting part: it's designed for \
travelers who are already at their destination and want to do something \
spontaneous. Type "what's available in Rome tomorrow" and get actual \
bookable tours with prices and times.

Could be an interesting follow-up to your AI travel testing piece — \
the tools are finally starting to do the booking part, not just the \
planning.

Search "Last Minute Tour Finder" in ChatGPT to try it.

John Anleitner
Last Minute Deals HQ
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
    outlet = target["outlet"]

    sent = _load_sent()
    if to_addr in sent:
        print(f"  SKIP {name} ({outlet}) — already sent")
        return False

    payload = {
        "personalizations": [{"to": [{"email": to_addr, "name": name}]}],
        "from": {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "reply_to": {"email": REPLY_TO, "name": EMAIL_FROM_NAME},
        "subject": target["subject"],
        "content": [{"type": "text/plain", "value": target["body"]}],
    }

    if dry_run:
        print(f"  DRY RUN → {name} <{to_addr}> ({outlet})")
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
        print(f"  SENT → {name} <{to_addr}> ({outlet})")
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

    print(f"Wave 2 outreach — {len(TARGETS)} targets")
    print(f"{'DRY RUN' if args.dry_run else 'LIVE SEND'}\n")

    sent_count = 0
    for target in TARGETS:
        if _send_email(target, dry_run=args.dry_run):
            sent_count += 1

    print(f"\nDone. {sent_count}/{len(TARGETS)} emails {'would be sent' if args.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
