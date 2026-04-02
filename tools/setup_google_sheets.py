"""
setup_google_sheets.py — One-time setup: create the Google Sheet with all required
tabs and column headers, then share it with the specified email.

Run once during initial setup. Never run again (it creates a new Sheet each time).

Usage:
    python tools/setup_google_sheets.py --title "Last Minute Deals" --email you@example.com

After running:
    Copy the printed GOOGLE_SHEET_ID into your .env file.
"""

import argparse
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Tab definitions ───────────────────────────────────────────────────────────
# Each tab: (name, [column headers])

SLOTS_HEADERS = [
    "slot_id", "platform", "business_id", "business_name",
    "category", "service_name",
    "start_time", "end_time", "duration_minutes", "hours_until_start",
    "price", "currency", "original_price",
    "our_price", "our_markup",
    "location_city", "location_state", "location_country",
    "latitude", "longitude",
    "booking_url",
    "scraped_at", "data_source", "confidence",
    "status",           # "active" | "expired" | "booked"
    "last_updated",
]

BOOKINGS_HEADERS = [
    "booking_id", "slot_id", "platform",
    "customer_name", "customer_email", "customer_phone",
    "service_name", "start_time", "location_city",
    "our_price", "our_markup",
    "stripe_payment_intent", "stripe_status",
    "platform_confirmation_number",
    "booked_at", "status",  # "confirmed" | "failed" | "refunded"
]

PRICING_LOG_HEADERS = [
    "slot_id", "platform", "category", "location_city",
    "hours_until_start", "original_price",
    "our_price", "our_markup", "markup_pct",
    "test_group",       # A/B test group identifier
    "converted",        # 1 if booking completed, 0 if not
    "recorded_at",
]

CUSTOMER_PREFS_HEADERS = [
    "customer_id", "name", "email", "phone",
    "cities",           # comma-separated: "NYC,Chicago"
    "categories",       # comma-separated: "wellness,beauty"
    "max_price",
    "max_hours_ahead",
    "sms_opt_in",       # TRUE/FALSE
    "email_opt_in",     # TRUE/FALSE
    "active",           # TRUE/FALSE — set FALSE to unsubscribe
    "created_at",
]

RUN_LOG_HEADERS = [
    "run_id", "started_at", "finished_at", "duration_seconds",
    "slots_fetched", "slots_new", "slots_updated", "slots_expired",
    "platforms_succeeded", "platforms_failed",
    "posts_twitter", "posts_reddit", "posts_telegram",
    "errors",
]

TABS = [
    ("Slots",          SLOTS_HEADERS),
    ("Bookings",       BOOKINGS_HEADERS),
    ("PricingLog",     PRICING_LOG_HEADERS),
    ("CustomerPrefs",  CUSTOMER_PREFS_HEADERS),
    ("RunLog",         RUN_LOG_HEADERS),
]


def get_credentials() -> Credentials:
    creds = None
    token_path = "token.json"
    creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if not os.path.exists(creds_path):
            print(f"ERROR: {creds_path} not found.")
            print("Download it from Google Cloud Console → APIs & Services → Credentials.")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def create_sheet(service, title: str) -> str:
    """Create a new Google Sheet and return its ID."""
    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"title": tab_name, "index": i}}
            for i, (tab_name, _) in enumerate(TABS)
        ],
    }
    result = service.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    return result["spreadsheetId"]


def write_headers(service, sheet_id: str) -> None:
    """Write column headers to each tab."""
    data = []
    for tab_name, headers in TABS:
        data.append({
            "range": f"{tab_name}!A1",
            "values": [headers],
        })

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def format_header_rows(service, sheet_id: str, spreadsheet: dict) -> None:
    """Bold and freeze the header row on every tab."""
    requests = []
    for sheet in spreadsheet["sheets"]:
        sid = sheet["properties"]["sheetId"]
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })
        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": requests},
    ).execute()


def share_sheet(drive_service, sheet_id: str, email: str) -> None:
    """Share the sheet with the given email as Editor."""
    drive_service.permissions().create(
        fileId=sheet_id,
        body={"type": "user", "role": "writer", "emailAddress": email},
        sendNotificationEmail=True,
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="One-time Google Sheets setup")
    parser.add_argument("--title", default="Last Minute Deals", help="Sheet title")
    parser.add_argument("--email", required=True, help="Email to share the sheet with")
    args = parser.parse_args()

    print("Authenticating with Google...")
    creds = get_credentials()

    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service  = build("drive",  "v3", credentials=creds)

    print(f"Creating sheet: '{args.title}'...")
    try:
        sheet_id = create_sheet(sheets_service, args.title)
    except HttpError as e:
        print(f"ERROR creating sheet: {e}")
        sys.exit(1)

    print("Writing headers...")
    write_headers(sheets_service, sheet_id)

    print("Fetching sheet metadata for formatting...")
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    format_header_rows(sheets_service, sheet_id, spreadsheet)

    print(f"Sharing with {args.email}...")
    share_sheet(drive_service, sheet_id, args.email)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    print("\n" + "=" * 60)
    print("Setup complete!")
    print(f"  Sheet URL : {sheet_url}")
    print(f"  Sheet ID  : {sheet_id}")
    print("\nAdd this line to your .env file:")
    print(f"  GOOGLE_SHEET_ID={sheet_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
