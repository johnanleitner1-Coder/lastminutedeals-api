"""
send_blogger_outreach.py — Email travel bloggers and AI tool reviewers.

These writers have published "best AI travel tools" articles but none of them
cover a tool that can actually BOOK tours.  Our pitch: we're the first
ChatGPT GPT (and MCP server) that books real tours from 40 suppliers.

Usage:
    python tools/send_blogger_outreach.py [--dry-run]
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

SENT_FILE = Path(__file__).parent / "seeds" / "blogger_outreach_sent.json"

# ── Targets ──────────────────────────────────────────────────────────────────

TARGETS = [
    {
        "name": "Paul Dhaliwal",
        "outlet": "CodeConductor",
        "to": "sales@codeconductor.ai",
        "article_url": "https://codeconductor.ai/blog/top-ai-travel-planning-tools/",
        "subject": "Your AI travel tools list is missing the one that actually books",
        "body": """\
Hi Paul,

I read your piece on the 14 best AI travel planning tools. Great roundup — \
but I noticed every tool on the list stops at planning. None of them can \
actually complete a booking.

We built one that does. Last Minute Tour Finder is a free Custom GPT in the \
ChatGPT Store that searches live inventory from 40 tour operators across \
100+ cities and lets customers book directly — glacier hikes in Iceland, \
cooking classes in Rome, Bosphorus cruises in Istanbul, desert safaris in \
Egypt. Real Stripe checkout, instant confirmation with the supplier.

It also works as an MCP server, so any AI agent (Claude, custom builds) \
can search and book tours programmatically. We're one of only 3 bookable \
tour MCP servers that exist right now.

Would you be open to trying it and potentially adding it to your list? \
Happy to give you a walkthrough or answer any questions.

Search "Last Minute Tour Finder" in the ChatGPT GPT Store to try it, or \
hit our API at https://api.lastminutedealshq.com/slots?city=Rome to see \
live inventory.

John Anleitner
Last Minute Deals HQ
https://lastminutedealshq.com
""",
    },
    {
        "name": "Gunnar",
        "outlet": "Thrifty Traveler",
        "to": "gunnar@thriftytraveler.com",
        "article_url": "https://thriftytraveler.com/guides/travel/best-trip-planning-ai-chatbots/",
        "subject": "Tested 5 AI chatbots for travel — but did any of them book anything?",
        "body": """\
Hi Gunnar,

Loved your piece comparing ChatGPT, Gemini, Claude, Deepseek, and CoPilot \
for trip planning. You nailed the core problem: they're good at \
recommendations but none of them can actually book anything.

We built the missing piece. Last Minute Tour Finder is a free Custom GPT \
in the ChatGPT Store that connects to live inventory from 40 local tour \
operators — Iceland, Rome, Istanbul, Cairo, Paris, Barcelona, and 100+ \
cities total. When a user finds something they like, they get a direct \
booking link with Stripe checkout and instant confirmation.

It's the difference between "here are some things to do in Rome" and \
"here's a walking tour tomorrow at 9am for EUR39 — click to book."

Would be great to see you test it alongside the others. Search "Last \
Minute Tour Finder" in ChatGPT to try it — no account or API key needed.

John Anleitner
Last Minute Deals HQ
https://lastminutedealshq.com
""",
    },
    {
        "name": "Mike",
        "outlet": "Mike's Road Trip",
        "to": "mike@mikesroadtrip.com",
        "article_url": "https://mikesroadtrip.com/ai-tools/",
        "subject": "AI tool suggestion: the first ChatGPT GPT that books real tours",
        "body": """\
Hi Mike,

You mentioned on your AI tools page that you're open to suggestions — \
here's one I think your audience would find interesting.

We built Last Minute Tour Finder, a free Custom GPT in the ChatGPT Store \
that actually books tours and activities. Not just recommendations — real \
live inventory from 40 local operators across 100+ cities (Iceland, Italy, \
Turkey, Egypt, Japan, Portugal, and more). Users search by city, see \
what's available in the next few days, and book directly with Stripe \
checkout and instant confirmation.

For travel bloggers specifically, it could be a useful tool to recommend \
to readers who are already at their destination and looking for something \
to do. The "last minute" angle means it surfaces tours departing in the \
next 72 hours with real availability.

It also works as an MCP server for AI agents — we're one of only 3 \
bookable tour APIs in the MCP ecosystem right now.

Search "Last Minute Tour Finder" in the ChatGPT Store to try it.

John Anleitner
Last Minute Deals HQ
https://lastminutedealshq.com
""",
    },
    {
        "name": "Michelle",
        "outlet": "AFAR Magazine",
        "to": "mbaran@afar.com",
        "article_url": "https://www.afar.com/magazine/we-tested-ai-travel-planning-apps-here-are-the-3-that-actually-worked",
        "subject": "You tested AI travel apps — here's one that actually books tours",
        "body": """\
Hi Michelle,

Your piece testing AI travel planning apps was spot-on — most of them are \
good at itineraries but stop short of actually getting you booked. That's \
the gap we're filling.

Last Minute Tour Finder is a free Custom GPT in the ChatGPT Store that \
searches live inventory from 40 local tour operators across 100+ cities \
and lets travelers book directly. Glacier hikes in Iceland, food tours in \
Istanbul, cooking classes in Rome, Nile cruises in Egypt — all with real \
Stripe checkout and instant confirmation from the operator.

The "last minute" focus means it's especially useful for travelers who are \
already at their destination: it surfaces tours departing in the next 72 \
hours that actually have availability. No more checking three apps and \
finding everything sold out.

If you're doing a follow-up or updating the piece, I'd love for you to \
give it a try. Search "Last Minute Tour Finder" in ChatGPT — no signup \
or API key needed.

John Anleitner
Last Minute Deals HQ
https://lastminutedealshq.com
""",
    },
    {
        "name": "Louis",
        "outlet": "Dupple",
        "to": "louis@dupple.com",
        "article_url": "https://dupple.com/learn/best-ai-for-trip-planning",
        "subject": "For your AI trip planning roundup: a GPT that actually books",
        "body": """\
Hi Louis,

In your AI trip planning roundup you mentioned thousands of GPTs in the \
Store for travel — but noted that most focus on itineraries rather than \
actual booking. You're right, and that's exactly what we built.

Last Minute Tour Finder is a free GPT in the ChatGPT Store connected to \
live inventory from 40 tour operators across 100+ cities. Users search by \
city, see real-time availability (sorted by soonest departure), and book \
directly via Stripe checkout with instant supplier confirmation.

Unlike Kayak or Layla which focus on flights and hotels, we're \
exclusively tours and activities — the piece of the trip that's hardest \
to plan and easiest to leave to the last minute.

It also runs as an MCP server, so developers building AI travel agents \
can plug in real tour booking without building supplier integrations.

Would be great to have it considered for your list. Search "Last Minute \
Tour Finder" in the GPT Store to try it.

John Anleitner
Last Minute Deals HQ
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
        print(f"    Body preview: {target['body'][:120]}...")
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
    parser = argparse.ArgumentParser(description="Email travel bloggers about Last Minute Tour Finder")
    parser.add_argument("--dry-run", action="store_true", help="Print emails without sending")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        sys.exit(1)

    print(f"Blogger outreach — {len(TARGETS)} targets")
    print(f"{'DRY RUN' if args.dry_run else 'LIVE SEND'}\n")

    sent_count = 0
    for target in TARGETS:
        if _send_email(target, dry_run=args.dry_run):
            sent_count += 1

    print(f"\nDone. {sent_count}/{len(TARGETS)} emails {'would be sent' if args.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
