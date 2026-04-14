"""
send_partner_outreach.py — Send partner/API access outreach emails via SendGrid.

Usage:
    python tools/send_partner_outreach.py [--dry-run]

Pass --dry-run to print email content without sending.
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

SENDGRID_API_KEY  = os.getenv("SENDGRID_API_KEY", "").strip()
EMAIL_FROM        = os.getenv("EMAIL_FROM", "bookings@lastminutedealshq.com")
EMAIL_FROM_NAME   = os.getenv("EMAIL_FROM_NAME", "Last Minute Deals")


EMAILS = [
    {
        "to":      "support@liquidspace.com",
        "subject": "API Integration Request — Last Minute Deals HQ",
        "body": """\
Hi LiquidSpace team,

My name is John, President of Last Minute Deals HQ (lastminutedealshq.com). We're building \
a marketplace that surfaces last-minute availability across multiple verticals and routes \
bookings to source platforms in real time.

We'd like to integrate with the LiquidSpace Marketplace API to include on-demand workspace \
and meeting room availability in our feed. We've reviewed your developer documentation and \
are ready to build — we just need API credentials to get started.

Could you provide us with:
- client_id
- client_secret
- Subscription key (Ocp-Apim-Subscription-Key)

Our use case: we surface short-notice (≤72h) available workspaces to users who book through \
our platform. We handle the reservation via your Marketplace API on the customer's behalf.

Company: Last Minute Deals HQ LLC
EIN: 41-5329371
Website: https://lastminutedealshq.com
Technical contact: bookings@lastminutedealshq.com

Happy to jump on a call if that's easier. Thank you!

John
Last Minute Deals HQ
""",
    },
    {
        "to":      "ben.smithart@peek.com",
        "subject": "Production API Key Request — Last Minute Deals HQ",
        "body": """\
Hi Ben,

Following up on our previous conversation — we now have our booking pipeline \
fully built and deployed on Railway, with Stripe payment processing, automated \
booking confirmation emails via SendGrid, and a live landing page at \
lastminutedealshq.com.

We're currently running against the Peek test environment (PEEK_API_KEY is set \
and our OCTO integration is working end-to-end). We're ready to go live and would \
love to get a production API key to start surfacing real Peek inventory.

Quick summary of our setup:
- Platform: OCTO-compliant (we use the standard Ventrata/Bokun/Peek OCTO endpoints)
- Base URL: https://octo.peek.com/integrations/octo
- Flow: availability search → Stripe auth → OCTO confirm → customer email
- Live API: https://api.lastminutedealshq.com

Company: Last Minute Deals HQ LLC
EIN: 41-5329371

Could you help us get a production key, or let me know what next steps look like \
on your end?

Thanks again for the help so far!

John
Last Minute Deals HQ
bookings@lastminutedealshq.com
""",
    },
]


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email via SendGrid Web API v3."""
    if not SENDGRID_API_KEY:
        return False

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
    parser = argparse.ArgumentParser(description="Send partner outreach emails")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print emails without sending")
    args = parser.parse_args()

    if not SENDGRID_API_KEY and not args.dry_run:
        print("ERROR: SENDGRID_API_KEY not set in .env")
        print("Add it and re-run, or use --dry-run to preview emails")
        sys.exit(1)

    for email in EMAILS:
        print(f"\n{'='*60}")
        print(f"TO:      {email['to']}")
        print(f"SUBJECT: {email['subject']}")
        print(f"{'─'*60}")
        print(email["body"])

        if args.dry_run:
            print("[DRY RUN] Not sent.")
            continue

        ok = send_email(email["to"], email["subject"], email["body"])
        if ok:
            print(f"[SENT] Email delivered to {email['to']}")
        else:
            print(f"[FAILED] Could not send to {email['to']}")

    print(f"\n{'='*60}")
    if not args.dry_run:
        print("Outreach complete.")


if __name__ == "__main__":
    main()
