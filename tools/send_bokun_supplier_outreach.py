"""
send_bokun_supplier_outreach.py — Email Bokun suppliers to request marketplace contracts.

We are a registered Bokun reseller. Suppliers connect by accepting our contract proposal
in their Bokun dashboard. This script emails Bokun suppliers to invite them to connect.

Usage:
    python tools/send_bokun_supplier_outreach.py [--dry-run]
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

# ── Batch 1 — April 9, 2026 ───────────────────────────────────────────────────
TARGETS = [
    {"name": "Venice Tours",                "to": "booking@venicetours.it",               "city": "Venice",       "country": "Italy",       "product": "cultural and historical city tours"},
    {"name": "REDRIB Experiences",          "to": "info@redrib.fi",                        "city": "Helsinki",     "country": "Finland",     "product": "RIB boat and adventure tours"},
    {"name": "Sherpa Food Tours",           "to": "hola@sherpafoodtours.com",             "city": "Buenos Aires", "country": "Argentina",   "product": "food and culinary tours"},
    {"name": "Bicycle Roma",                "to": "info@bicycleroma.com",                  "city": "Rome",         "country": "Italy",       "product": "bicycle tours"},
    {"name": "City Rome Tours",             "to": "info@cityrometours.com",                "city": "Rome",         "country": "Italy",       "product": "walking and cultural tours"},
    {"name": "Urban Saunters",              "to": "info@urbansaunters.com",                "city": "London",       "country": "UK",          "product": "walking tours"},
    {"name": "Lisbon Surfaris",             "to": "lisbonsurfaris@gmail.com",             "city": "Lisbon",       "country": "Portugal",    "product": "surf lessons and tours"},
    {"name": "LivTours",                    "to": "info@livtours.com",                     "city": "Rome",         "country": "Italy",       "product": "cultural and historical tours"},
    {"name": "Stoke Travel",               "to": "info@stoketravel.com",                  "city": "Barcelona",    "country": "Spain",       "product": "adventure travel and tours"},
    {"name": "VivaRoma Tours",              "to": "info@vivaromatours.com",                "city": "Rome",         "country": "Italy",       "product": "walking and city tours"},
    {"name": "Sandbar Joe's",              "to": "hellosandbarjoe@gmail.com",            "city": "Wildwood",     "country": "USA",         "product": "pontoon boat tours"},
    {"name": "Tijon Miami",                "to": "miami@tijon.com",                       "city": "Coral Gables", "country": "USA",         "product": "fragrance and experience classes"},
    {"name": "Railbiking in Greece",        "to": "railbiking@gmail.com",                 "city": "Greece",       "country": "Greece",      "product": "rail biking tours"},
    {"name": "Sailing Windermere",          "to": "info@sailingwindermere.co.uk",          "city": "Windermere",   "country": "UK",          "product": "sailing tours"},
    {"name": "Boka Bliss Boats",           "to": "info@bokablissboats.me",               "city": "Kotor",        "country": "Montenegro",  "product": "boat tours"},
    {"name": "Montenegro Water Tours",      "to": "montenegrowatertours@gmail.com",       "city": "Tivat",        "country": "Montenegro",  "product": "water tours and boat rentals"},
    {"name": "Monster Day Tours",           "to": "info@monsterdaytours.com",             "city": "Singapore",    "country": "Singapore",   "product": "food tours"},
    {"name": "Japan Guide Agency",          "to": "booking@j-g-a.org",                    "city": "Shizuoka",     "country": "Japan",       "product": "private guided tours"},
    {"name": "BK Wine Tours",              "to": "info@bkwine.com",                      "city": "Paris",        "country": "France",      "product": "wine tours"},
    {"name": "Kualoa Ranch",               "to": "reservation@kualoa.com",              "city": "Kaneohe",      "country": "USA",         "product": "adventure and zipline tours"},
    {"name": "Sonoma Zipline Adventures",   "to": "info@sonomacanopytours.com",           "city": "Occidental",   "country": "USA",         "product": "zipline and canopy tours"},
    {"name": "Northwoods Zipline",          "to": "info@northwoodszipline.com",           "city": "Minocqua",     "country": "USA",         "product": "zipline and adventure tours"},
    {"name": "The Canyons Zip Line",        "to": "info@zipthecanyons.com",               "city": "Ocala",        "country": "USA",         "product": "zipline and adventure park"},
    {"name": "Orlando Tree Trek",           "to": "info@orlandotreetrek.com",             "city": "Orlando",      "country": "USA",         "product": "zipline and ropes course"},
    {"name": "Coral Triangle Adventures",   "to": "info@coraltriangleadventures.com",     "city": "Indonesia",    "country": "Indonesia",   "product": "snorkeling and dive tours"},
    # ── Batch 6 — April 11, 2026 — NYC & Chicago top operators ──────────────────
    {"name": "HeliNY",                     "to": "info@heliny.com",                      "city": "New York",     "country": "USA",         "product": "helicopter sightseeing tours over Manhattan"},
    {"name": "New York Media Boat",        "to": "tours@NYmediaBoat.com",               "city": "New York",     "country": "USA",         "product": "speedboat and sightseeing tours"},
    {"name": "The Escape Game NYC",        "to": "NewYorkCity@TheEscapeGame.com",        "city": "New York",     "country": "USA",         "product": "immersive escape room experiences"},
    {"name": "Fly Heli Chicago",           "to": "info@flyhelitours.com",                "city": "Chicago",      "country": "USA",         "product": "helicopter sightseeing tours over Chicago"},
    {"name": "360 CHICAGO",               "to": "info@360chicago.com",                  "city": "Chicago",      "country": "USA",         "product": "observation deck and TILT experiences"},
    {"name": "Skydeck Chicago",            "to": "sales@theskydeck.com",                 "city": "Chicago",      "country": "USA",         "product": "observation deck and Ledge glass floor experiences"},
    {"name": "First Lady Cruises",         "to": "chartersales@FirstLady.com",           "city": "Chicago",      "country": "USA",         "product": "architecture river cruises"},
    {"name": "The Escape Game Chicago",    "to": "Hello@TheEscapeGame.com",              "city": "Chicago",      "country": "USA",         "product": "immersive escape room experiences"},
    {"name": "Urban Kayaks Chicago",       "to": "office@urbankayaks.com",               "city": "Chicago",      "country": "USA",         "product": "kayak tours on the Chicago River and Lake Michigan"},
    # ── Batch 5 — April 11, 2026 — High-scored operators from GYG/Viator research ──
    # NOTE: DIVE.IS, Elding, Special Tours, Mountaineers of Iceland use Bokun
    #       — send them contract proposals via Bokun dashboard instead
    {"name": "Anthelion Helicopters",      "to": "info@anthelionhelicopters.com",        "city": "Long Beach",   "country": "USA",         "product": "helicopter sightseeing tours"},
    {"name": "Gondola Getaway",            "to": "gondolier1@verizon.net",               "city": "Long Beach",   "country": "USA",         "product": "gondola cruises"},
    {"name": "Norðurflug Helicopter Tours","to": "info@helicopter.is",                   "city": "Reykjavik",    "country": "Iceland",     "product": "helicopter sightseeing tours over Iceland"},
    {"name": "Into the Glacier",           "to": "info@intotheglacier.is",               "city": "Húsafell",     "country": "Iceland",     "product": "ice cave and glacier tunnel tours"},
    {"name": "Arctic Adventures",          "to": "info@adventures.is",                   "city": "Reykjavik",    "country": "Iceland",     "product": "glacier hikes, ice caves, and adventure tours"},
    {"name": "Ishestar",                   "to": "info@ishestar.is",                     "city": "Hafnarfjörður","country": "Iceland",     "product": "Icelandic horse riding tours"},
    {"name": "Thames Rockets",             "to": "bookings@thamesrockets.com",           "city": "London",       "country": "UK",          "product": "high-speed RIB speedboat tours on the Thames"},
    {"name": "Secret Food Tours London",   "to": "contact@secrettours.com",              "city": "London",       "country": "UK",          "product": "small-group food walking tours"},
    {"name": "Hungry Birds Amsterdam",     "to": "hello@hungrybirds.nl",                 "city": "Amsterdam",    "country": "Netherlands", "product": "street food tours"},
    {"name": "Wetlands Safari",            "to": "info@wetlandssafari.nl",               "city": "Amsterdam",    "country": "Netherlands", "product": "guided canoe tours through the Waterland wetlands"},
    {"name": "Eating Europe Amsterdam",    "to": "info@eatingeurope.com",                "city": "Amsterdam",    "country": "Netherlands", "product": "neighborhood food walking tours"},
    {"name": "Gray Line Iceland",          "to": "iceland@grayline.is",                  "city": "Reykjavik",    "country": "Iceland",     "product": "Golden Circle, Northern Lights, and South Coast tours"},
    {"name": "EastWest Iceland",           "to": "info@eastwest.is",                     "city": "Reykjavik",    "country": "Iceland",     "product": "small-group Golden Circle and Northern Lights tours"},
    # ── Batch 4 — April 9, 2026 ───────────────────────────────────────────────
    {"name": "Glacier Adventure",           "to": "custom@glacieradventure.is",           "city": "Höfn",         "country": "Iceland",     "product": "glacier tours"},
    {"name": "Ice Lagoon Jökulsárlón",      "to": "info@jokulsarlon.is",                  "city": "Höfn",         "country": "Iceland",     "product": "glacier lagoon boat tours"},
    {"name": "Arctic Adventures",           "to": "custom@adventures.is",                 "city": "Reykjavik",    "country": "Iceland",     "product": "adventure tours"},
    # ── Batch 3 — April 9, 2026 ───────────────────────────────────────────────
    {"name": "Simba Sea Trips",             "to": "info@simbaseatrips.com",               "city": "Phuket",       "country": "Thailand",    "product": "luxury boat tours and sea trips"},
    {"name": "DC Metro Food Tours",         "to": "info@foodtourcorp.com",                "city": "Washington DC","country": "USA",         "product": "walking food tours and cooking classes"},
    {"name": "Hello Nature Adventure Tours","to": "adventures@hellonature.ca",            "city": "Ucluelet",     "country": "Canada",      "product": "sea kayaking and hiking tours"},
    {"name": "Sea to Sky Expeditions",      "to": "info@seatoskyexpeditions.com",         "city": "Cowichan Bay", "country": "Canada",      "product": "kayaking, canoeing, and rafting tours"},
    {"name": "Maple Leaf Adventures",       "to": "info@mapleleafadventures.com",         "city": "Victoria",     "country": "Canada",      "product": "expedition cruises and kayaking tours"},
    {"name": "Aqua Tours Bovec",            "to": "aquatoursbovec@gmail.com",             "city": "Bovec",        "country": "Slovenia",    "product": "rafting, canyoning, and kayaking tours"},
    {"name": "Bokun Charter Agency",        "to": "info@thebokun.com",                    "city": "Omiš",         "country": "Croatia",     "product": "boat tours and island excursions"},
    {"name": "Dolphin Discovery",           "to": "helloIslaMujeres@dolphindiscovery.com","city": "Isla Mujeres", "country": "Mexico",      "product": "dolphin encounters and water activities"},
    {"name": "Whale Watch Kaikoura",        "to": "res@whalewatch.co.nz",                 "city": "Kaikōura",     "country": "New Zealand",  "product": "whale watching tours"},
    {"name": "Coastal Bliss Adventures",    "to": "coastalbliss@shaw.ca",                 "city": "Cowichan Bay", "country": "Canada",      "product": "kayaking, hiking, and canoeing tours"},
    {"name": "Kingfisher Wilderness",       "to": "info@kingfisher.ca",                   "city": "Port McNeill", "country": "Canada",      "product": "sea kayaking expeditions"},
    {"name": "Mare e Vento Favignana",      "to": "info@marevento.it",                    "city": "Favignana",    "country": "Italy",       "product": "boat tours of the Egadi Islands"},
    # ── Batch 2 — April 9, 2026 ───────────────────────────────────────────────
    {"name": "Mega Zipline Iceland",        "to": "info@megazipline.is",                  "city": "Hveragerði",   "country": "Iceland",     "product": "zipline and aerial adventure"},
    {"name": "Tasty Tours NYC",             "to": "info@tastytoursnyc.com",               "city": "New York",     "country": "USA",         "product": "food tours"},
    {"name": "Naviera Nortour",             "to": "info@navieranortour.com",              "city": "Fuerteventura","country": "Spain",        "product": "ferry and island excursions"},
    {"name": "Hullu Poro",                  "to": "sales@hulluporo.fi",                   "city": "Levi",         "country": "Finland",     "product": "reindeer sleigh tours"},
    {"name": "Troll.is",                    "to": "info@troll.is",                         "city": "Reykjavik",    "country": "Iceland",     "product": "glacier and super jeep adventure tours"},
    {"name": "Hidden Iceland",              "to": "sarah@hiddeniceland.is",               "city": "Reykjavik",    "country": "Iceland",     "product": "luxury adventure tours"},
    {"name": "Reykjavik Excursions",        "to": "info@re.is",                            "city": "Reykjavik",    "country": "Iceland",     "product": "coach and bus tours"},
    {"name": "Adventure Tours Norway",      "to": "post@adventuretours.no",               "city": "Skjolden",     "country": "Norway",      "product": "fjord and hiking tours"},
]

EMAIL_TEMPLATE = """\
Hi {name} team,

I'm John, President of Last Minute Deals HQ — an API-first booking platform \
built for AI agents and developers. We expose tour and activity inventory \
through a REST API and MCP server so AI assistants (Claude, ChatGPT, custom \
travel bots) can search and book {product} automatically. No existing OTA \
offers this. We're the infrastructure layer for AI-powered travel planning, \
focused on last-minute availability (≤72h).

We're a registered Bokun reseller. Connecting takes ~2 minutes in your Bokun \
dashboard — accept our contract proposal and your inventory is live on our \
platform. No commitment, no fees on your end.

Want your {product} in front of the next wave of AI booking agents?

lastminutedealshq.com | bookings@lastminutedealshq.com

John
President, Last Minute Deals HQ
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


SENT_LOG = Path(__file__).parent / "seeds" / "bokun_sent_emails.json"


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

    # Deduplicate and skip already-sent addresses
    seen = set()
    targets = []
    for t in TARGETS:
        if t["to"] not in seen and t["to"] not in sent:
            seen.add(t["to"])
            targets.append(t)

    if not targets:
        print("No new targets to send to.")
        return

    print(f"{len(targets)} new targets (skipping {len(sent)} already sent)")

    newly_sent = set()
    for target in targets:
        subject = f"AI Booking Partnership — {target['name']} x Last Minute Deals HQ"
        body    = EMAIL_TEMPLATE.format(
            name=target["name"],
            product=target["product"],
        )

        print(f"\n{'='*60}")
        print(f"TO:      {target['to']}")
        print(f"SUBJECT: {subject}")
        print(f"{'─'*60}")
        print(body)

        if args.dry_run:
            print("[DRY RUN] Not sent.")
            continue

        ok = send_email(target["to"], subject, body)
        print(f"[{'SENT' if ok else 'FAILED'}] {target['to']}")
        if ok:
            newly_sent.add(target["to"])

    if not args.dry_run:
        save_sent(sent | newly_sent)

    print(f"\n{'='*60}")
    if not args.dry_run:
        print(f"Outreach complete. {len(targets)} emails sent.")


if __name__ == "__main__":
    main()
