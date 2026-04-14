"""
send_ventrata_new_batch.py — New batch of Ventrata supplier outreach emails.

Fresh targets compiled April 2026 — all confirmed not in bokun_sent_emails.json
or ventrata_followup_sent.json. Focused on Ventrata-connected operators across
North America, Europe, and major tourism hubs.

Usage:
    python tools/send_ventrata_new_batch.py [--dry-run]
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

# ── New Batch — April 14, 2026 — Fresh Ventrata-connected suppliers ────────────
# All verified NOT in bokun_sent_emails.json or ventrata_followup_sent.json
TARGETS = [
    # ── North America: High-volume city operators ─────────────────────────────
    {"name": "Brooklyn Bridge Park Boathouse",    "to": "info@bbpboathouse.org",                    "city": "New York",        "country": "USA",          "product": "kayaking and waterfront tours"},
    {"name": "Circle Line Sightseeing Cruises",  "to": "info@circleline.com",                      "city": "New York",        "country": "USA",          "product": "Manhattan island boat cruises"},
    {"name": "NYC Ferry",                        "to": "partnerships@ferry.nyc",                   "city": "New York",        "country": "USA",          "product": "commuter and sightseeing ferry rides"},
    {"name": "Gray Line New York",               "to": "reservations@graylinenewyork.com",         "city": "New York",        "country": "USA",          "product": "hop-on hop-off bus tours of Manhattan"},
    {"name": "Adirondack Scenic Railroad",       "to": "info@adirondackrr.com",                    "city": "Utica",           "country": "USA",          "product": "scenic train excursions"},
    {"name": "Hornblower Cruises NYC",           "to": "groups@cityexperiences.com",               "city": "New York",        "country": "USA",          "product": "dinner and sightseeing cruises"},
    {"name": "Spirit of Boston",                 "to": "groups@cityexperiences.com",               "city": "Boston",          "country": "USA",          "product": "dinner and sightseeing harbor cruises"},
    {"name": "Go San Francisco",                 "to": "info@gocity.com",                          "city": "San Francisco",   "country": "USA",          "product": "city attraction passes and tours"},
    {"name": "Alcatraz City Cruises",            "to": "alcatraz@cityexperiences.com",             "city": "San Francisco",   "country": "USA",          "product": "Alcatraz Island ferry and tours"},
    {"name": "Old Town Trolley Tours",           "to": "info@trolleytours.com",                    "city": "San Diego",       "country": "USA",          "product": "narrated trolley sightseeing tours"},
    {"name": "San Diego Harbor Excursion",       "to": "info@flagshipsd.com",                      "city": "San Diego",       "country": "USA",          "product": "whale watching and harbor cruises"},
    {"name": "Airboat Rides at Midway",          "to": "info@airboatridesat.com",                  "city": "Orlando",         "country": "USA",          "product": "Florida Everglades airboat tours"},
    {"name": "Eco Tours of Florida",             "to": "info@ecotoursofflorida.com",               "city": "Miami",           "country": "USA",          "product": "Everglades eco tours and kayaking"},
    {"name": "Miami Duck Tours",                 "to": "info@miamiduckadventures.com",             "city": "Miami",           "country": "USA",          "product": "amphibious duck boat city tours"},
    {"name": "Nashville Pedal Tavern",           "to": "info@pedaltavern.com",                     "city": "Nashville",       "country": "USA",          "product": "pedal pub party bike tours"},
    {"name": "General Jackson Showboat",         "to": "groups@generaljackson.com",                "city": "Nashville",       "country": "USA",          "product": "riverboat dinner and music cruises"},
    {"name": "New Orleans Steamboat Company",    "to": "reservations@steamboatnatchez.com",        "city": "New Orleans",     "country": "USA",          "product": "Mississippi River jazz dinner cruises"},
    {"name": "Swamp Tour New Orleans",           "to": "info@louisianaswamptours.com",             "city": "New Orleans",     "country": "USA",          "product": "bayou swamp airboat tours"},
    {"name": "Denver Landmark Tours",            "to": "info@denverwalkingtour.com",               "city": "Denver",          "country": "USA",          "product": "downtown walking tours"},
    {"name": "Rocky Mountain Adventures",        "to": "info@shoprmoa.com",                        "city": "Fort Collins",    "country": "USA",          "product": "whitewater rafting and kayaking tours"},
    {"name": "Seattle Seaplanes",                "to": "info@seattleseaplanes.com",                "city": "Seattle",         "country": "USA",          "product": "scenic seaplane tours over Puget Sound"},
    {"name": "Ariel's Grotto Wine Country",      "to": "info@nwwinecountry.com",                   "city": "Woodinville",     "country": "USA",          "product": "wine country bus tours"},
    {"name": "Napa Valley Wine Train",           "to": "groupsales@winetrain.com",                 "city": "Napa",            "country": "USA",          "product": "gourmet wine country rail experiences"},
    {"name": "Sunset Ranch Hollywood",           "to": "info@sunsetranchhollywood.com",            "city": "Los Angeles",     "country": "USA",          "product": "Hollywood Hills horseback riding tours"},
    {"name": "Bike and Roll Chicago",            "to": "info@bikechicago.com",                     "city": "Chicago",         "country": "USA",          "product": "lakefront bike tours and rentals"},
    # ── Canada ────────────────────────────────────────────────────────────────
    {"name": "Niagara Helicopters",              "to": "info@niagarahelicopters.com",              "city": "Niagara Falls",   "country": "Canada",       "product": "helicopter sightseeing over Niagara Falls"},
    {"name": "Niagara City Cruises",             "to": "info@niagaracruises.com",                  "city": "Niagara Falls",   "country": "Canada",       "product": "Niagara Falls boat tours"},
    {"name": "Vancouver Whale Watch",            "to": "info@vancouverwhalewatching.com",           "city": "Vancouver",       "country": "Canada",       "product": "whale watching and marine wildlife tours"},
    {"name": "FlyOver Canada",                   "to": "info@flyovercanada.com",                   "city": "Vancouver",       "country": "Canada",       "product": "immersive flight ride attraction"},
    {"name": "Rocky Mountaineer",                "to": "groups@rockymountaineer.com",              "city": "Vancouver",       "country": "Canada",       "product": "luxury scenic rail journeys through the Rockies"},
    # ── UK & Ireland ──────────────────────────────────────────────────────────
    {"name": "Mersey Ferries",                   "to": "info@merseyferries.co.uk",                 "city": "Liverpool",       "country": "UK",           "product": "river Mersey ferry cruises and Beatles tours"},
    {"name": "City Sightseeing Edinburgh",       "to": "info@edinburghtour.com",                   "city": "Edinburgh",       "country": "UK",           "product": "hop-on hop-off bus tours of Edinburgh"},
    {"name": "Rabbie's Trail Burners",           "to": "info@rabbies.com",                         "city": "Edinburgh",       "country": "UK",           "product": "small-group tours of Scotland and Ireland"},
    {"name": "City Sightseeing Liverpool",       "to": "info@citysightseeingliverpool.com",        "city": "Liverpool",       "country": "UK",           "product": "hop-on hop-off bus tours"},
    {"name": "Magna Carta Experience York",      "to": "bookings@visityork.org",                   "city": "York",            "country": "UK",           "product": "historic city walking tours"},
    {"name": "Paddywagon Tours",                 "to": "info@paddywagontours.com",                 "city": "Dublin",          "country": "Ireland",      "product": "hop-on hop-off and day tours across Ireland"},
    {"name": "Dublin Bike Tours",                "to": "info@dublinbiketours.com",                 "city": "Dublin",          "country": "Ireland",      "product": "guided cycling tours of Dublin"},
    # ── Continental Europe ────────────────────────────────────────────────────
    {"name": "Bateaux Parisiens",                 "to": "info@bateauxparisiens.com",                "city": "Paris",           "country": "France",       "product": "Seine River dinner and sightseeing cruises"},
    {"name": "Paris Segway Tours",               "to": "info@parissegwaytours.com",                "city": "Paris",           "country": "France",       "product": "Segway guided tours of Paris"},
    {"name": "Amsterdam Boat Center",            "to": "info@amsterdamboatcenter.com",             "city": "Amsterdam",       "country": "Netherlands",  "product": "canal boat rentals and guided tours"},
    {"name": "Lovers Canal Cruises",             "to": "info@lovers.nl",                           "city": "Amsterdam",       "country": "Netherlands",  "product": "canal sightseeing and dinner cruises"},
    {"name": "City Sightseeing Barcelona",       "to": "info@barcelonabusturistic.com",            "city": "Barcelona",       "country": "Spain",        "product": "hop-on hop-off bus tours of Barcelona"},
    {"name": "Barcelona Segway Tours",           "to": "info@barcelonasegway.com",                 "city": "Barcelona",       "country": "Spain",        "product": "Segway guided tours of Barcelona"},
    {"name": "Flamenco Tickets Madrid",          "to": "info@tablao-flamenco.com",                 "city": "Madrid",          "country": "Spain",        "product": "live flamenco show tickets"},
    {"name": "Rome City Sightseeing",            "to": "info@rome.citysightseeing.it",             "city": "Rome",            "country": "Italy",        "product": "hop-on hop-off bus tours of Rome"},
    {"name": "Ziplining in Interlaken",          "to": "info@verticallife.ch",                     "city": "Interlaken",      "country": "Switzerland",  "product": "zipline and adventure park activities"},
    {"name": "Bernese Oberland Paragliding",     "to": "info@paragliding-interlaken.ch",           "city": "Interlaken",      "country": "Switzerland",  "product": "tandem paragliding over the Swiss Alps"},
    # ── Asia-Pacific ─────────────────────────────────────────────────────────
    {"name": "Sydney Harbour Tall Ship",         "to": "info@sydneytallships.com.au",              "city": "Sydney",          "country": "Australia",    "product": "tall ship sailing tours on Sydney Harbour"},
    {"name": "BridgeClimb Sydney",               "to": "groups@bridgeclimb.com",                   "city": "Sydney",          "country": "Australia",    "product": "Sydney Harbour Bridge guided climbs"},
    {"name": "Tokyo Great Cycling Tour",          "to": "info@tokyocycling.jp",                     "city": "Tokyo",           "country": "Japan",        "product": "guided cycling tours of Tokyo neighborhoods"},
]

EMAIL_SUBJECT = "Last-minute slot distribution — zero risk, incremental revenue"

EMAIL_TEMPLATE = """\
Hi {name} team,

I'm John, President of Last Minute Deals HQ (lastminutedealshq.com). We're an \
AI-powered booking platform that distributes last-minute tour and activity slots \
— inventory within 72 hours that would otherwise expire unsold. Our system \
surfaces your availability to AI agents (Claude, ChatGPT, custom travel bots) \
and direct consumers searching for same-day and next-day experiences.

We connect via OCTO — the industry standard used by Ventrata, Bokun, and Peek. \
If you're on Ventrata, connecting takes about 2 minutes: generate an API key in \
your Ventrata dashboard and share it with us. We only sell slots you haven't \
filled; you keep your existing pricing and take zero risk. No contracts, no \
upfront fees — we make money only when we move your inventory.

We're already live with Big Bus Tours, Fat Tire Tours, Gray Line, and 80+ other \
Ventrata operators. Would you be open to connecting {name} to our distribution \
feed? Happy to walk through the setup on a quick call if easier.

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


BOKUN_SENT_LOG    = Path(__file__).parent / "seeds" / "bokun_sent_emails.json"
VENTRATA_SENT_LOG = Path(__file__).parent / "seeds" / "ventrata_followup_sent.json"
NEW_BATCH_LOG     = Path(__file__).parent / "seeds" / "ventrata_new_batch_sent.json"


def load_all_sent() -> set:
    combined = set()
    for path in [BOKUN_SENT_LOG, VENTRATA_SENT_LOG, NEW_BATCH_LOG]:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            combined.update(e.lower() for e in data)
    return combined


def save_new_batch(sent: set):
    NEW_BATCH_LOG.write_text(json.dumps(sorted(sent), indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print emails without sending")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        sys.exit(1)

    already_sent = load_all_sent()

    # Deduplicate within this batch and skip any previously contacted address
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
        print("Skipping the following:")
        for s in skipped:
            print(s)
        print()

    if not targets:
        print("No new targets to send to.")
        return

    print(f"Ready to send: {len(targets)} emails")
    print(f"Already contacted (skipped): {len(skipped)}")
    print()

    newly_sent = set()
    for i, target in enumerate(targets, 1):
        body = EMAIL_TEMPLATE.format(name=target["name"])
        print(f"{'='*60}")
        print(f"[{i}/{len(targets)}] TO:      {target['to']}")
        print(f"              NAME:    {target['name']} ({target['city']}, {target['country']})")
        print(f"              SUBJECT: {EMAIL_SUBJECT}")
        print(f"{'─'*60}")
        print(body)

        if args.dry_run:
            print("[DRY RUN] Not sent.")
            continue

        ok = send_email(target["to"], EMAIL_SUBJECT, body)
        print(f"[{'SENT' if ok else 'FAILED'}] {target['to']}")
        if ok:
            newly_sent.add(target["to"].lower())

    print(f"\n{'='*60}")
    if not args.dry_run:
        save_new_batch(newly_sent)
        print(f"Outreach complete. {len(newly_sent)} emails sent.")
        print(f"Sent log saved to: {NEW_BATCH_LOG}")
    else:
        print(f"DRY RUN complete. {len(targets)} emails previewed.")


if __name__ == "__main__":
    main()
