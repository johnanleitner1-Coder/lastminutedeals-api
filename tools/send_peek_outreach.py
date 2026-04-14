"""
send_peek_outreach.py — Outreach to Peek Pro operators to connect their API key.

Peek Pro operators each control their own OCTO API key (per Ben Smithart). This
script contacts operators known to use Peek Pro and asks them to share their key
so we can distribute their last-minute inventory.

Usage:
    python tools/send_peek_outreach.py [--dry-run]
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
EMAIL_FROM_NAME  = os.getenv("EMAIL_FROM_NAME", "Last Minute Deals")

# Peek Pro operators — sourced from Peek Pro case studies, partner pages, and
# known users of the platform. Each controls their own OCTO API key via their
# Peek Pro dashboard (confirmed by Peek Pro team, April 2026).
TARGETS = [
    # ── Adventure / Outdoor ──────────────────────────────────────────────────
    {"name": "Mammoth Mountain Ski Area",        "to": "groupsales@mammoth.com",            "city": "Mammoth Lakes",    "country": "USA"},
    {"name": "Vail Mountain Adventures",         "to": "vailinfo@vailresorts.com",          "city": "Vail",             "country": "USA"},
    {"name": "Colorado Whitewater Rafting",      "to": "info@coloradowhitewater.com",       "city": "Buena Vista",      "country": "USA"},
    {"name": "Arizona Rafting Adventures",       "to": "info@azraft.com",                   "city": "Flagstaff",        "country": "USA"},
    {"name": "Moab Adventure Center",            "to": "info@moabadventurecenter.com",      "city": "Moab",             "country": "USA"},
    {"name": "Jackson Hole Whitewater",          "to": "info@jacksonholewhitewater.com",    "city": "Jackson",          "country": "USA"},
    {"name": "Zion Adventure Company",           "to": "info@zionadventures.com",           "city": "Springdale",       "country": "USA"},
    {"name": "Hawaii Forest and Trail",          "to": "info@hawaii-forest.com",            "city": "Kailua-Kona",      "country": "USA"},
    {"name": "Roberts Hawaii Tours",             "to": "info@robertshawaii.com",            "city": "Honolulu",         "country": "USA"},
    {"name": "Maui Ocean Center",                "to": "info@mauioceancenter.com",          "city": "Maalaea",          "country": "USA"},
    # ── Water / Marine ───────────────────────────────────────────────────────
    {"name": "Whale Watch Western Australia",    "to": "info@whalewatch.com.au",            "city": "Dunsborough",      "country": "Australia"},
    {"name": "Captain Dave's Dolphin Safari",    "to": "info@dolphinsafari.com",            "city": "Dana Point",       "country": "USA"},
    {"name": "Channel Islands Outfitters",       "to": "info@channelislandsoutfitters.com", "city": "Santa Barbara",    "country": "USA"},
    {"name": "Monterey Bay Whale Watch",         "to": "info@montereybaywhalewatch.com",    "city": "Monterey",         "country": "USA"},
    {"name": "Sea Trek Sausalito",               "to": "info@seatrekkayak.com",             "city": "Sausalito",        "country": "USA"},
    {"name": "Kauai Sea Tours",                  "to": "info@kauaiseatours.com",            "city": "Port Allen",       "country": "USA"},
    # ── Scenic / Sightseeing ─────────────────────────────────────────────────
    {"name": "Grand Canyon Railway",             "to": "info@thetrain.com",                 "city": "Williams",         "country": "USA"},
    {"name": "Antelope Canyon Tours",            "to": "info@antelopecanyon.com",           "city": "Page",             "country": "USA"},
    {"name": "Arches Jeep Tours",                "to": "info@archesjeepadventures.com",     "city": "Moab",             "country": "USA"},
    {"name": "Papillon Grand Canyon Helicopters","to": "info@papillon.com",                 "city": "Las Vegas",        "country": "USA"},
    {"name": "Maverick Helicopters",             "to": "info@maverickhelicopter.com",       "city": "Las Vegas",        "country": "USA"},
    {"name": "Blue Hawaiian Helicopters",        "to": "info@bluehawaiian.com",             "city": "Kahului",          "country": "USA"},
    # ── City Tours / Cultural ────────────────────────────────────────────────
    {"name": "Haunted History Tours New Orleans","to": "info@hauntedhistorytours.com",      "city": "New Orleans",      "country": "USA"},
    {"name": "Savannah Taste Experience",        "to": "info@savannahtasteexperience.com",  "city": "Savannah",         "country": "USA"},
    {"name": "Free Tours by Foot NYC",           "to": "info@freetoursbyfoot.com",          "city": "New York",         "country": "USA"},
    {"name": "Urban Adventures Chicago",         "to": "info@urbanadventures.com",          "city": "Chicago",          "country": "USA"},
    {"name": "Boston Duck Tours",                "to": "info@bostonducktours.com",           "city": "Boston",           "country": "USA"},
    {"name": "Old Town Trolley Boston",          "to": "info@historictours.com",            "city": "Boston",           "country": "USA"},
    {"name": "Portland Food Cart Tours",         "to": "info@portlandfoodcarttours.com",    "city": "Portland",         "country": "USA"},
    {"name": "Austin Food Tours",                "to": "info@austinfoodtours.com",          "city": "Austin",           "country": "USA"},
    # ── Skiing / Snow ────────────────────────────────────────────────────────
    {"name": "Park City Mountain Resort",        "to": "info@parkcitymountain.com",         "city": "Park City",        "country": "USA"},
    {"name": "Keystone Resort",                  "to": "groupsales@keystoneresort.com",     "city": "Keystone",         "country": "USA"},
    {"name": "Big Sky Resort",                   "to": "info@bigskyresort.com",             "city": "Big Sky",          "country": "USA"},
    {"name": "Whistler Blackcomb",               "to": "info@whistlerblackcomb.com",        "city": "Whistler",         "country": "Canada"},
    # ── Zip / Canopy / Aerial ────────────────────────────────────────────────
    {"name": "CLIMB Works Maui",                 "to": "info@climbworks.com",               "city": "Haiku",            "country": "USA"},
    {"name": "Zip World",                        "to": "info@zipworld.co.uk",               "city": "Bethesda",         "country": "UK"},
    {"name": "Navitat Canopy Adventures",        "to": "info@navitat.com",                  "city": "Asheville",        "country": "USA"},
    {"name": "Skyline Eco-Adventures",           "to": "info@skyline-eco-adventures.com",   "city": "Kaanapali",        "country": "USA"},
    # ── Escape Rooms / Indoor Experiences ───────────────────────────────────
    {"name": "The Escape Game Nashville",        "to": "info@theescapegame.com",            "city": "Nashville",        "country": "USA"},
    {"name": "60Out Escape Rooms",               "to": "info@60out.com",                    "city": "Los Angeles",      "country": "USA"},
    {"name": "Puzzle Break Seattle",             "to": "info@puzzlebreak.us",               "city": "Seattle",          "country": "USA"},
    # ── Wine / Food ──────────────────────────────────────────────────────────
    {"name": "Old Town Temecula Wine Tours",     "to": "info@grapeline.us",                 "city": "Temecula",         "country": "USA"},
    {"name": "Sonoma Wine Country Tours",        "to": "info@sonomacountrytours.com",       "city": "Sonoma",           "country": "USA"},
    {"name": "Portland Wine Tours",              "to": "info@portlandwinetours.com",        "city": "Portland",         "country": "USA"},
    # ── Cycling ──────────────────────────────────────────────────────────────
    {"name": "Backroads Bike Tours",             "to": "info@backroads.com",                "city": "Berkeley",         "country": "USA"},
    {"name": "Trek Travel",                      "to": "info@trektravel.com",               "city": "Madison",          "country": "USA"},
    {"name": "Cycling Escapes",                  "to": "info@cyclingescapes.com",           "city": "Sedona",           "country": "USA"},
    # ── Miscellaneous High-Volume ────────────────────────────────────────────
    {"name": "Original Haunted Houses Dallas",   "to": "info@originalhaunted.com",          "city": "Dallas",           "country": "USA"},
    {"name": "Go Ape USA",                       "to": "info@goape.com",                    "city": "National",         "country": "USA"},
]

EMAIL_SUBJECT = "Last-minute slot distribution — zero risk, incremental revenue"

EMAIL_TEMPLATE = """\
Hi {name} team,

I'm John, President of Last Minute Deals HQ (lastminutedealshq.com). We're an \
AI-powered booking platform specializing in last-minute tour and activity slots \
— inventory within 72 hours that would otherwise expire unsold. We surface your \
availability to AI agents (Claude, ChatGPT, custom travel bots) and consumers \
actively searching for same-day and next-day experiences.

We connect via OCTO through Peek Pro — if you're already on Peek Pro, connecting \
takes about 2 minutes: generate an API key in your Peek Pro dashboard and share \
it with us. We only distribute slots you haven't filled; you keep your existing \
pricing and take zero risk. No contracts, no upfront fees — we earn only when we \
move your inventory.

We're signing on new Peek Pro operators every week across adventure, outdoor, \
sightseeing, and cultural experiences. Would you be open to connecting {name} \
to our distribution feed? Happy to send setup instructions or jump on a quick \
call if that's easier.

lastminutedealshq.com | bookings@lastminutedealshq.com

John Anleitner
President, Last Minute Deals HQ LLC
"""


def send_email(to: str, subject: str, body: str) -> bool:
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from":             {"email": EMAIL_FROM, "name": EMAIL_FROM_NAME},
        "reply_to":         {"email": "bookings@inbound.lastminutedealshq.com", "name": EMAIL_FROM_NAME},
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


SENT_LOGS = [
    Path(__file__).parent / "seeds" / "bokun_sent_emails.json",
    Path(__file__).parent / "seeds" / "ventrata_followup_sent.json",
    Path(__file__).parent / "seeds" / "ventrata_new_batch_sent.json",
]
PEEK_SENT_LOG = Path(__file__).parent / "seeds" / "peek_outreach_sent.json"


def load_all_sent() -> set:
    combined = set()
    for path in SENT_LOGS + [PEEK_SENT_LOG]:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            combined.update(e.lower() for e in data)
    return combined


def save_sent(sent: set):
    PEEK_SENT_LOG.write_text(json.dumps(sorted(sent), indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print emails without sending")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        sys.exit(1)

    already_sent = load_all_sent()

    seen = set()
    targets = []
    skipped = []
    for t in TARGETS:
        key = t["to"].lower()
        if key in seen:
            skipped.append(f"  DUPE:  {t['to']} ({t['name']})")
            continue
        seen.add(key)
        if key in already_sent:
            skipped.append(f"  SKIP:  {t['to']} ({t['name']}) — already contacted")
            continue
        targets.append(t)

    if skipped:
        print("Skipping:")
        for s in skipped:
            print(s)
        print()

    if not targets:
        print("No new targets to send to.")
        return

    print(f"Ready to send: {len(targets)} emails\n")

    newly_sent = set()
    for i, target in enumerate(targets, 1):
        body = EMAIL_TEMPLATE.format(name=target["name"])
        print(f"[{i}/{len(targets)}] {target['to']}  ({target['name']}, {target['city']})")

        if args.dry_run:
            print("  [DRY RUN]")
            continue

        ok = send_email(target["to"], EMAIL_SUBJECT, body)
        print(f"  [{'SENT' if ok else 'FAILED'}]")
        if ok:
            newly_sent.add(target["to"].lower())

    print(f"\nDone. {len(newly_sent) if not args.dry_run else len(targets)} emails {'sent' if not args.dry_run else 'previewed'}.")
    if not args.dry_run and newly_sent:
        save_sent(newly_sent)
        print(f"Log saved to: {PEEK_SENT_LOG}")


if __name__ == "__main__":
    main()
