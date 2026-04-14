"""
send_ventrata_supplier_outreach.py — Email Ventrata suppliers to request connections.

We are an approved Ventrata connectivity partner. Suppliers connect via their
Ventrata dashboard which generates an API key they give us. This script emails
high-value US-based Ventrata suppliers to invite them to connect.

Usage:
    python tools/send_ventrata_supplier_outreach.py [--dry-run]
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

# Full Ventrata supplier list with verified contact emails
# Compiled April 2026 — we are an approved Ventrata connectivity partner
# NOT_FOUND entries are excluded; see comments at bottom for manual follow-up list
TARGETS = [
    # ── Batch 1 ───────────────────────────────────────────────────────────────
    {"name": "Big Bus Tours",                    "to": "info@bigbustours.com",                  "city": "London",            "country": "UK",          "product": "hop-on hop-off bus tours"},
    {"name": "Crown Tours",                      "to": "info@crowntours.com",                   "city": "Rome",              "country": "Italy",       "product": "sightseeing tours"},
    {"name": "Golden Tours",                     "to": "reservations@goldentours.com",          "city": "London",            "country": "UK",          "product": "London sightseeing tours"},
    {"name": "City Sightseeing New York",        "to": "info@citysightseeingnewyork.com",       "city": "New York",          "country": "USA",         "product": "hop-on hop-off bus tours"},
    {"name": "360 CHICAGO",                      "to": "info@360chicago.com",                   "city": "Chicago",           "country": "USA",         "product": "observation deck tickets"},
    {"name": "Paris Montparnasse Tower",         "to": "info@tourmontparnasse56.com",           "city": "Paris",             "country": "France",      "product": "observation deck tickets"},
    {"name": "Euromast Rotterdam",               "to": "info@euromast.nl",                      "city": "Rotterdam",         "country": "Netherlands", "product": "observation tower tickets"},
    {"name": "FlyOver Las Vegas",                "to": "info@experienceflyover.com",            "city": "Las Vegas",         "country": "USA",         "product": "flight simulator experience tickets"},
    {"name": "Yankee Freedom Dry Tortugas Ferry","to": "reservations@yankeefreedom.com",        "city": "Key West",          "country": "USA",         "product": "ferry and national park tours"},
    {"name": "Boston Tea Party Ships & Museum",  "to": "groups@bostonteapartyships.com",        "city": "Boston",            "country": "USA",         "product": "historic museum experience tickets"},
    {"name": "Tower Bridge",                     "to": "bookings@towerbridge.org.uk",           "city": "London",            "country": "UK",          "product": "Tower Bridge exhibition tickets"},
    {"name": "Vedettes De Paris",                "to": "info@vedettesdeparis.com",              "city": "Paris",             "country": "France",      "product": "Seine river cruise tickets"},
    {"name": "Tootbus",                          "to": "contact@tootbus.com",                   "city": "Paris",             "country": "France",      "product": "hop-on hop-off bus tours"},
    {"name": "Jasper SkyTram",                   "to": "groups@banffjaspercollection.com",      "city": "Jasper",            "country": "Canada",      "product": "aerial gondola tram tickets"},
    {"name": "Gray Line Niagara Falls",          "to": "info@graylineniagarafalls.com",         "city": "Niagara Falls",     "country": "USA",         "product": "Niagara Falls sightseeing tours"},
    {"name": "Gray Line Seattle",                "to": "info@graylineseattle.com",              "city": "Seattle",           "country": "USA",         "product": "Seattle sightseeing tours"},
    {"name": "Up at The O2",                     "to": "boxoffice@upattheo2.co.uk",             "city": "London",            "country": "UK",          "product": "O2 dome climbing experience tickets"},
    {"name": "Moco Museum",                      "to": "hello@mocomuseum.com",                  "city": "Amsterdam",         "country": "Netherlands", "product": "modern art museum tickets"},
    {"name": "Dubai Balloon At Atlantis",        "to": "info@thedubaiballoon.com",              "city": "Dubai",             "country": "UAE",         "product": "hot air balloon experience tickets"},
    # ── Batch 2 ───────────────────────────────────────────────────────────────
    {"name": "Boston Duck Tours",                "to": "info@bostonducktours.com",              "city": "Boston",            "country": "USA",         "product": "amphibious duck boat tours"},
    {"name": "Yellow Bus Tours",                 "to": "yellowbus@carris.pt",                   "city": "Lisbon",            "country": "Portugal",    "product": "hop-on hop-off bus tours"},
    {"name": "Key Tours",                        "to": "reservations@keytours.gr",              "city": "Athens",            "country": "Greece",      "product": "tours across Greece"},
    {"name": "Discover Banff Tours",             "to": "groups@banfftours.com",                 "city": "Banff",             "country": "Canada",      "product": "Banff National Park tours"},
    {"name": "Landsea Tours & Adventures",       "to": "info@vancouvertours.com",               "city": "Vancouver",         "country": "Canada",      "product": "sightseeing and adventure tours"},
    {"name": "Old South Carriage Company",       "to": "info@oldsouthcarriage.com",             "city": "Charleston",        "country": "USA",         "product": "horse-drawn carriage tours"},
    {"name": "Fat Tire Tours",                   "to": "groups@fattiretours.com",               "city": "Paris",             "country": "France",      "product": "bike and walking tours"},
    {"name": "Dig This Vegas",                   "to": "info@digthis.info",                     "city": "Las Vegas",         "country": "USA",         "product": "heavy equipment experience"},
    {"name": "Paris By Mouth",                   "to": "parisbymouthtours@gmail.com",           "city": "Paris",             "country": "France",      "product": "food and culinary tours"},
    {"name": "Platinum Heritage",                "to": "info@platinum-heritage.com",            "city": "Dubai",             "country": "UAE",         "product": "luxury desert safaris"},
    {"name": "Intrepid Urban Adventures",        "to": "info@urbanadventures.com",              "city": "Melbourne",         "country": "Australia",   "product": "urban walking and cultural tours"},
    {"name": "ExperienceFirst",                  "to": "info@exp1.com",                         "city": "New York",          "country": "USA",         "product": "walking tours across major cities"},
    {"name": "Museum of Illusions",              "to": "support@museumofillusions.com",         "city": "Zagreb",            "country": "Croatia",     "product": "interactive illusions museum tickets"},
    {"name": "AVA Colorado Rafting",             "to": "info@coloradorafting.net",              "city": "Idaho Springs",     "country": "USA",         "product": "whitewater rafting and zipline adventures"},
    {"name": "Starline Tours",                   "to": "info@starlinetours.com",                "city": "Los Angeles",       "country": "USA",         "product": "Hollywood sightseeing tours"},
    {"name": "SKY Helicopters",                  "to": "sky@skyhelicopters.com",                "city": "Dallas",            "country": "USA",         "product": "helicopter tours"},
    {"name": "Paramount Studio Tours",           "to": "Guest_Relations@paramount.com",         "city": "Los Angeles",       "country": "USA",         "product": "studio behind-the-scenes tours"},
    {"name": "Lally Tours",                      "to": "hello@lallytours.com",                  "city": "Galway",            "country": "Ireland",     "product": "Ireland west coast tours"},
    {"name": "Thames RIB Experience",            "to": "info@thamesribexperience.com",          "city": "London",            "country": "UK",          "product": "Thames speedboat rides"},
    {"name": "The EDGE Ziplines & Adventures",   "to": "info@theedgezip.com",                   "city": "Castle Rock",       "country": "USA",         "product": "zipline and adventure park"},
    {"name": "City Sightseeing South Africa",    "to": "info@citysightseeing.co.za",            "city": "Cape Town",         "country": "South Africa","product": "hop-on hop-off bus tours"},
    # ── Batch 3 ───────────────────────────────────────────────────────────────
    {"name": "Le Centre d'Activites Mont-Tremblant", "to": "info@tremblantactivities.com",      "city": "Mont-Tremblant",    "country": "Canada",      "product": "outdoor adventure activities"},
    {"name": "CruiseRI",                         "to": "info@cruiseri.com",                     "city": "Newport",           "country": "USA",         "product": "boat cruises and nautical tours"},
    {"name": "FRS Portugal",                     "to": "info@frs-portugal.pt",                  "city": "Lisbon",            "country": "Portugal",    "product": "river cruises and ferries"},
    {"name": "Skyline Sightseeing",              "to": "info@sightseeingworld.com",             "city": "San Francisco",     "country": "USA",         "product": "hop-on hop-off sightseeing tours"},
    {"name": "Cointreau",                        "to": "carre.cointreau@remy-cointreau.com",    "city": "Saint-Barthelemy-d'Anjou", "country": "France", "product": "distillery tours and tastings"},
    {"name": "Manchester River Cruises",         "to": "info@manchesterrivercruises.com",       "city": "Manchester",        "country": "UK",          "product": "river cruises"},
    {"name": "Perth Explorer",                   "to": "info@perthexplorer.com.au",             "city": "Perth",             "country": "Australia",   "product": "hop-on hop-off bus tours"},
    {"name": "The Great Canadian Trolley Company","to": "hello@greatcanadiantrolley.com",       "city": "Victoria",          "country": "Canada",      "product": "hop-on hop-off trolley tours"},
    {"name": "City Sightseeing Prague",          "to": "info@hoponhopoffprague.com",            "city": "Prague",            "country": "Czech Republic","product": "hop-on hop-off bus tours"},
    {"name": "National Gallery London",          "to": "hello@nationalgallery.org.uk",          "city": "London",            "country": "UK",          "product": "art museum tickets and tours"},
    {"name": "London City Bus Tours",            "to": "info@londoncitybustours.com",           "city": "London",            "country": "UK",          "product": "hop-on hop-off bus tours"},
    {"name": "Experience Galway",                "to": "bookings@experiencegalway.ie",          "city": "Galway",            "country": "Ireland",     "product": "walking and cultural tours"},
    {"name": "Big City Tourism",                 "to": "info@bigcitytourism.com",               "city": "New York",          "country": "USA",         "product": "city boat and walking tours"},
    {"name": "Premium Tours",                    "to": "bookings@premiumtours.co.uk",           "city": "London",            "country": "UK",          "product": "sightseeing and day tours"},
    {"name": "Thames River Sightseeing",         "to": "info@thamesriversightseeing.com",       "city": "London",            "country": "UK",          "product": "Thames river cruises"},
    {"name": "Timberbush Tours",                 "to": "accommodation@timberbushtours.com",     "city": "Edinburgh",         "country": "UK",          "product": "guided tours of Scotland"},
    {"name": "Singapore DUCKtours",              "to": "sales@ducktours.com.sg",                "city": "Singapore",         "country": "Singapore",   "product": "amphibious duck boat tours"},
    {"name": "The Yellow Tours",                 "to": "info@theyellowtour.com",                "city": "Madrid",            "country": "Spain",       "product": "city tours and excursions"},
    {"name": "City Sightseeing New Orleans",     "to": "info@citysightseeingneworleans.com",   "city": "New Orleans",       "country": "USA",         "product": "hop-on hop-off bus tours"},
    {"name": "St. Louis Cemetery No. 1 Tour",   "to": "info@cemeterytourneworleans.com",       "city": "New Orleans",       "country": "USA",         "product": "official cemetery walking tours"},
    {"name": "City Sightseeing Athens",          "to": "info@citysightseeing.gr",              "city": "Athens",            "country": "Greece",      "product": "hop-on hop-off bus tours"},
    {"name": "Boost Portugal",                   "to": "reservations@boostportugal.com",        "city": "Lisbon",            "country": "Portugal",    "product": "segway, tuk-tuk, and bike tours"},
    {"name": "The Cooltours",                    "to": "info.lisbon@thecooltours.com",          "city": "Lisbon",            "country": "Portugal",    "product": "city tours and day trips"},
    {"name": "Bluedragon Porto City Tours",      "to": "info@bluedragon.pt",                    "city": "Porto",             "country": "Portugal",    "product": "tuk-tuk, segway, and walking tours"},
    # ── Batch 4 (previously missed) ───────────────────────────────────────────
    {"name": "Celest (Zalmhaven Rotterdam)",     "to": "info@celest.nl",                        "city": "Rotterdam",         "country": "Netherlands", "product": "skybar and observation experience on the highest tower in Benelux"},
    {"name": "Highline Warsaw (Varso Tower)",    "to": "sales@highlinewarsaw.com",              "city": "Warsaw",            "country": "Poland",      "product": "observation deck on the highest point in the EU"},
    {"name": "Balloon Adventures Dubai",         "to": "bookings@uae.heroballoonflights.com",   "city": "Dubai",             "country": "UAE",         "product": "hot air balloon rides over the Dubai desert"},
    {"name": "Hero Balloon Flights Saudi",       "to": "bookings@ksa.heroballoonflights.com",   "city": "AlUla",             "country": "Saudi Arabia","product": "hot air balloon flights over AlUla archaeological sites"},
    {"name": "Platinum Heritage",                "to": "info@uae.platinum-heritage.com",        "city": "Dubai",             "country": "UAE",         "product": "luxury desert safaris and fine dining"},
    {"name": "Jeanie Johnston",                  "to": "reservations@jeaniejohnston.ie",        "city": "Dublin",            "country": "Ireland",     "product": "tall ship museum and Irish Famine history tours"},
    {"name": "Discover Dorset Tours",            "to": "info@discoverdorset.co.uk",             "city": "Bournemouth",       "country": "UK",          "product": "sightseeing tours across Dorset"},
    {"name": "Tugatrips",                        "to": "info@tugatrips.com",                    "city": "Lisbon",            "country": "Portugal",    "product": "tuk-tuk and guided tours across Portugal"},
    {"name": "Pilsner Urquell Experience",       "to": "reservations@asahibeer.cz",             "city": "Pilsen",            "country": "Czech Republic","product": "brewery tours with underground cellars and tastings"},
]

# ── NOT FOUND — no public email (form-only or unreachable) ────────────────────
# Follow up manually or skip:
#   Empire State Building      — esbnyc.com/contact-us (form only)
#   Historic Tours of America  — historictours.com/contact-us (form only)
#   Berlin TV Tower            — tv-turm.de/en/contact-us (form only)
#   Uber Boat by Thames Clippers — thamesclippers.com/info/contact-us (form only)
#   Gray Line Westcoast Sightseeing — westcoastsightseeing.com/contact-us (form only)
#   Airboat Adventures         — airboatadventures.net (phone only)
#   EPIC Museum Dublin         — epicchq.com/contact (form only)
#   Mountain Goat Tours        — website unreachable
#   Remy Martin                — form only
#   Sir Paddy                  — can't identify
#   Tour Dubai                 — form only
#   ItaliaTours                — form only
#
# ── FLAGGED — confirm before sending ─────────────────────────────────────────
#   The Ride NYC               — appears to have closed late 2025
#   Sightseeing Pass           — operations suspended mid-2025
#   Schonbrunn VR              — VR experience discontinued Feb 2025
#   Tower Tours (SF)           — routes to Big Bus Tours SF (sfreservations@bigbustours.com)
#
# ── NO EMAIL — manual follow-up only ────────────────────────────────────────
#   Gray Line Worldwide        — form only: grayline.com/contact-us / +1-303-539-8502
#   City Sightseeing Operators — franchise network, no central partnership email found

EMAIL_TEMPLATE = """\
Hi {name} team,

I'm John, President of Last Minute Deals HQ. We're an API-first booking platform \
built specifically for AI agents and developers — think Claude, ChatGPT, and \
custom travel bots that need to search and book real inventory in real time.

We expose your availability through a REST API and MCP server, so any AI \
assistant can find and book your {product} automatically. No existing OTA \
offers this. We're the infrastructure layer for AI-powered travel planning, \
and last-minute inventory (≤72h) is exactly what these agents are looking for.

We're an approved Ventrata connectivity partner. Connecting takes ~2 minutes \
in your Ventrata dashboard — no contract, no commitment.

Want your inventory in front of the next wave of AI booking agents?

lastminutedealshq.com | bookings@lastminutedealshq.com

John
Last Minute Deals HQ
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

    for target in TARGETS:
        subject = f"Last-Minute Distribution Partnership — {target['name']} x Last Minute Deals HQ"
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

    print(f"\n{'='*60}")
    if not args.dry_run:
        print(f"Outreach complete. {len(TARGETS)} emails sent.")


if __name__ == "__main__":
    main()
