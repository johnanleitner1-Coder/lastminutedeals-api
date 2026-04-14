"""
send_ventrata_followup.py — One-time follow-up to Ventrata supplier contacts.

Sends to the same list as send_ventrata_supplier_outreach.py.
Subject and body updated to lead with the AI agent distribution angle.

Usage:
    python tools/send_ventrata_followup.py [--dry-run]
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

# Same list as original outreach — send follow-up only to these contacts
TARGETS = [
    # ── Batch 1 ───────────────────────────────────────────────────────────────
    {"name": "Big Bus Tours",                    "to": "info@bigbustours.com",                  "product": "hop-on hop-off bus tours"},
    {"name": "Crown Tours",                      "to": "info@crowntours.com",                   "product": "sightseeing tours"},
    {"name": "Golden Tours",                     "to": "reservations@goldentours.com",          "product": "London sightseeing tours"},
    {"name": "City Sightseeing New York",        "to": "info@citysightseeingnewyork.com",       "product": "hop-on hop-off bus tours"},
    {"name": "360 CHICAGO",                      "to": "info@360chicago.com",                   "product": "observation deck tickets"},
    {"name": "Paris Montparnasse Tower",         "to": "info@tourmontparnasse56.com",           "product": "observation deck tickets"},
    {"name": "Euromast Rotterdam",               "to": "info@euromast.nl",                      "product": "observation tower tickets"},
    {"name": "FlyOver Las Vegas",                "to": "info@experienceflyover.com",            "product": "flight simulator experience tickets"},
    {"name": "Yankee Freedom Dry Tortugas Ferry","to": "reservations@yankeefreedom.com",        "product": "ferry and national park tours"},
    {"name": "Boston Tea Party Ships & Museum",  "to": "groups@bostonteapartyships.com",        "product": "historic museum experience tickets"},
    {"name": "Tower Bridge",                     "to": "bookings@towerbridge.org.uk",           "product": "Tower Bridge exhibition tickets"},
    {"name": "Vedettes De Paris",                "to": "info@vedettesdeparis.com",              "product": "Seine river cruise tickets"},
    {"name": "Tootbus",                          "to": "contact@tootbus.com",                   "product": "hop-on hop-off bus tours"},
    {"name": "Jasper SkyTram",                   "to": "groups@banffjaspercollection.com",      "product": "aerial gondola tram tickets"},
    {"name": "Gray Line Niagara Falls",          "to": "info@graylineniagarafalls.com",         "product": "Niagara Falls sightseeing tours"},
    {"name": "Gray Line Seattle",                "to": "info@graylineseattle.com",              "product": "Seattle sightseeing tours"},
    {"name": "Up at The O2",                     "to": "boxoffice@upattheo2.co.uk",             "product": "O2 dome climbing experience tickets"},
    {"name": "Moco Museum",                      "to": "hello@mocomuseum.com",                  "product": "modern art museum tickets"},
    {"name": "Dubai Balloon At Atlantis",        "to": "info@thedubaiballoon.com",              "product": "hot air balloon experience tickets"},
    # ── Batch 2 ───────────────────────────────────────────────────────────────
    {"name": "Boston Duck Tours",                "to": "info@bostonducktours.com",              "product": "amphibious duck boat tours"},
    {"name": "Yellow Bus Tours",                 "to": "yellowbus@carris.pt",                   "product": "hop-on hop-off bus tours"},
    {"name": "Key Tours",                        "to": "reservations@keytours.gr",              "product": "tours across Greece"},
    {"name": "Discover Banff Tours",             "to": "groups@banfftours.com",                 "product": "Banff National Park tours"},
    {"name": "Landsea Tours & Adventures",       "to": "info@vancouvertours.com",               "product": "sightseeing and adventure tours"},
    {"name": "Old South Carriage Company",       "to": "info@oldsouthcarriage.com",             "product": "horse-drawn carriage tours"},
    {"name": "Fat Tire Tours",                   "to": "groups@fattiretours.com",               "product": "bike and walking tours"},
    {"name": "Dig This Vegas",                   "to": "info@digthis.info",                     "product": "heavy equipment experience"},
    {"name": "Paris By Mouth",                   "to": "parisbymouthtours@gmail.com",           "product": "food and culinary tours"},
    {"name": "Platinum Heritage",                "to": "info@platinum-heritage.com",            "product": "luxury desert safaris"},
    {"name": "Intrepid Urban Adventures",        "to": "info@urbanadventures.com",              "product": "urban walking and cultural tours"},
    {"name": "ExperienceFirst",                  "to": "info@exp1.com",                         "product": "walking tours across major cities"},
    {"name": "Museum of Illusions",              "to": "support@museumofillusions.com",         "product": "interactive illusions museum tickets"},
    {"name": "AVA Colorado Rafting",             "to": "info@coloradorafting.net",              "product": "whitewater rafting and zipline adventures"},
    {"name": "Starline Tours",                   "to": "info@starlinetours.com",                "product": "Hollywood sightseeing tours"},
    {"name": "SKY Helicopters",                  "to": "sky@skyhelicopters.com",                "product": "helicopter tours"},
    {"name": "Paramount Studio Tours",           "to": "Guest_Relations@paramount.com",         "product": "studio behind-the-scenes tours"},
    {"name": "Lally Tours",                      "to": "hello@lallytours.com",                  "product": "Ireland west coast tours"},
    {"name": "Thames RIB Experience",            "to": "info@thamesribexperience.com",          "product": "Thames speedboat rides"},
    {"name": "The EDGE Ziplines & Adventures",   "to": "info@theedgezip.com",                   "product": "zipline and adventure park"},
    {"name": "City Sightseeing South Africa",    "to": "info@citysightseeing.co.za",            "product": "hop-on hop-off bus tours"},
    {"name": "Le Centre d'Activites Mont-Tremblant", "to": "info@tremblantactivities.com",      "product": "outdoor adventure activities"},
    {"name": "CruiseRI",                         "to": "info@cruiseri.com",                     "product": "boat cruises and nautical tours"},
    {"name": "FRS Portugal",                     "to": "info@frs-portugal.pt",                  "product": "river cruises and ferries"},
    {"name": "Skyline Sightseeing",              "to": "info@sightseeingworld.com",             "product": "hop-on hop-off sightseeing tours"},
    {"name": "Cointreau",                        "to": "carre.cointreau@remy-cointreau.com",    "product": "distillery tours and tastings"},
    {"name": "Manchester River Cruises",         "to": "info@manchesterrivercruises.com",       "product": "river cruises"},
    {"name": "Perth Explorer",                   "to": "info@perthexplorer.com.au",             "product": "hop-on hop-off bus tours"},
    {"name": "The Great Canadian Trolley Company","to": "hello@greatcanadiantrolley.com",       "product": "hop-on hop-off trolley tours"},
    {"name": "City Sightseeing Prague",          "to": "info@hoponhopoffprague.com",            "product": "hop-on hop-off bus tours"},
    {"name": "National Gallery London",          "to": "hello@nationalgallery.org.uk",          "product": "art museum tickets and tours"},
    {"name": "London City Bus Tours",            "to": "info@londoncitybustours.com",           "product": "hop-on hop-off bus tours"},
    {"name": "Experience Galway",                "to": "bookings@experiencegalway.ie",          "product": "walking and cultural tours"},
    {"name": "Big City Tourism",                 "to": "info@bigcitytourism.com",               "product": "city boat and walking tours"},
    {"name": "Premium Tours",                    "to": "bookings@premiumtours.co.uk",           "product": "sightseeing and day tours"},
    {"name": "Thames River Sightseeing",         "to": "info@thamesriversightseeing.com",       "product": "Thames river cruises"},
    {"name": "Timberbush Tours",                 "to": "accommodation@timberbushtours.com",     "product": "guided tours of Scotland"},
    {"name": "Singapore DUCKtours",              "to": "sales@ducktours.com.sg",                "product": "amphibious duck boat tours"},
    {"name": "The Yellow Tours",                 "to": "info@theyellowtour.com",                "product": "city tours and excursions"},
    {"name": "City Sightseeing New Orleans",     "to": "info@citysightseeingneworleans.com",   "product": "hop-on hop-off bus tours"},
    {"name": "St. Louis Cemetery No. 1 Tour",   "to": "info@cemeterytourneworleans.com",       "product": "official cemetery walking tours"},
    {"name": "City Sightseeing Athens",          "to": "info@citysightseeing.gr",              "product": "hop-on hop-off bus tours"},
    {"name": "Boost Portugal",                   "to": "reservations@boostportugal.com",        "product": "segway, tuk-tuk, and bike tours"},
    {"name": "The Cooltours",                    "to": "info.lisbon@thecooltours.com",          "product": "city tours and day trips"},
    {"name": "Bluedragon Porto City Tours",      "to": "info@bluedragon.pt",                    "product": "tuk-tuk, segway, and walking tours"},
    # ── Batch 4 ───────────────────────────────────────────────────────────────
    {"name": "Celest (Zalmhaven Rotterdam)",     "to": "info@celest.nl",                        "product": "skybar and observation experience"},
    {"name": "Highline Warsaw (Varso Tower)",    "to": "sales@highlinewarsaw.com",              "product": "observation deck experience"},
    {"name": "Balloon Adventures Dubai",         "to": "bookings@uae.heroballoonflights.com",   "product": "hot air balloon rides"},
    {"name": "Hero Balloon Flights Saudi",       "to": "bookings@ksa.heroballoonflights.com",   "product": "hot air balloon flights"},
    {"name": "Platinum Heritage UAE",            "to": "info@uae.platinum-heritage.com",        "product": "luxury desert safaris and fine dining"},
    {"name": "Jeanie Johnston",                  "to": "reservations@jeaniejohnston.ie",        "product": "tall ship museum and history tours"},
    {"name": "Discover Dorset Tours",            "to": "info@discoverdorset.co.uk",             "product": "sightseeing tours across Dorset"},
    {"name": "Tugatrips",                        "to": "info@tugatrips.com",                    "product": "tuk-tuk and guided tours"},
    {"name": "Pilsner Urquell Experience",       "to": "reservations@asahibeer.cz",             "product": "brewery tours with underground cellars and tastings"},
]

SENT_LOG = Path(__file__).parent / "seeds" / "ventrata_followup_sent.json"

def _load_sent() -> set:
    if SENT_LOG.exists():
        try:
            return set(json.loads(SENT_LOG.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()

def _save_sent(sent: set) -> None:
    SENT_LOG.write_text(json.dumps(sorted(sent), indent=2), encoding="utf-8")


FOLLOWUP_TEMPLATE = """\
Hi {name} team,

Following up on my note from last week. Wanted to give you a more concrete \
picture of what we're building.

We are the booking layer for AI agents. When someone asks Claude, ChatGPT, \
or a custom travel assistant to "find last-minute {product} available this \
weekend," our platform is what searches real inventory, checks availability, \
and executes the booking — in real time, automatically. No OTA does this. \
We built the infrastructure specifically for it: a REST API plus an MCP server \
that any AI assistant can call directly.

What that means for you: your unsold last-minute slots get surfaced to \
AI-powered travelers at the exact moment they are ready to book. Not browsing, \
not comparing — booking.

Setup takes about two minutes in your Ventrata dashboard. We are an approved \
Ventrata connectivity partner, so everything is already in place on our end. \
There is no contract and no commitment.

Is there a better contact on your team to send this to, or would you like \
more details on how the connection works?

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        sys.exit(1)

    already_sent = _load_sent()
    sent = failed = skipped = 0
    for target in TARGETS:
        if target["to"] in already_sent:
            print(f"[SKIP] Already sent: {target['to']}")
            skipped += 1
            continue

        subject = f"Re: Last-Minute AI Booking Distribution — {target['name']}"
        body    = FOLLOWUP_TEMPLATE.format(
            name=target["name"],
            product=target["product"],
        )

        print(f"\n{'='*60}")
        print(f"TO:      {target['to']}")
        print(f"SUBJECT: {subject}")
        print(f"{'─'*60}")
        print(body[:300] + "...")

        if args.dry_run:
            print("[DRY RUN] Not sent.")
            sent += 1
            continue

        ok = send_email(target["to"], subject, body)
        if ok:
            print(f"[SENT] {target['to']}")
            already_sent.add(target["to"])
            _save_sent(already_sent)
            sent += 1
        else:
            print(f"[FAILED] {target['to']}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Done. Sent: {sent}  Failed: {failed}  Skipped: {skipped}  Total: {len(TARGETS)}")


if __name__ == "__main__":
    main()
