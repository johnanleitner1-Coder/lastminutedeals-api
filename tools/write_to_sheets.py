"""
write_to_sheets.py — Upsert aggregated slots into Google Sheets.

Behavior:
  - Reads aggregated_slots.json
  - Upserts rows keyed on slot_id (updates existing rows, appends new ones)
  - Marks slots with hours_until_start <= 0 as "expired" in-place
  - Appends a run summary row to the RunLog tab
  - NEVER deletes rows — expired slots are flagged with status="expired"

Usage:
    python tools/write_to_sheets.py \
        [--data-file .tmp/aggregated_slots.json] \
        [--sheet-id SHEET_ID]

Credentials:
    GOOGLE_SHEET_ID in .env (overridden by --sheet-id)
    token.json + credentials.json for OAuth
"""

import argparse
import json
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_LIBS_AVAILABLE = True
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False

DATA_FILE = Path(".tmp/aggregated_slots.json")
SCOPES    = ["https://www.googleapis.com/auth/spreadsheets"]

# Column order must match SLOTS_HEADERS in setup_google_sheets.py exactly
SLOTS_COLUMNS = [
    "slot_id", "platform", "business_id", "business_name",
    "category", "service_name",
    "start_time", "end_time", "duration_minutes", "hours_until_start",
    "price", "currency", "original_price",
    "our_price", "our_markup",
    "location_city", "location_state", "location_country",
    "latitude", "longitude",
    "booking_url",
    "scraped_at", "data_source", "confidence",
    "status", "last_updated",
]

SLOT_ID_COL = 0   # column A (0-indexed)
STATUS_COL  = SLOTS_COLUMNS.index("status")
UPDATED_COL = SLOTS_COLUMNS.index("last_updated")


def get_credentials() -> Credentials:
    creds = None
    token_path = "token.json"
    creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Silently refresh using the refresh token — never open a browser popup
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    # Only open browser if we truly have no valid credentials at all
    if not creds or not creds.valid:
        if not Path(creds_path).exists():
            print(f"ERROR: {creds_path} not found — cannot authenticate with Google.")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def slot_to_row(slot: dict) -> list:
    """Convert a slot dict to a row list in SLOTS_COLUMNS order."""
    now = datetime.now(timezone.utc).isoformat()
    row = [str(slot.get(col, "") or "") for col in SLOTS_COLUMNS]
    # Set status and last_updated
    row[STATUS_COL]  = slot.get("status", "active")
    row[UPDATED_COL] = now
    return row


def fetch_existing_rows(service, sheet_id: str) -> tuple[list[list], dict[str, int]]:
    """
    Fetch all rows from the Slots tab.
    Returns (all_rows, {slot_id: row_index}) where row_index is 1-based (Sheet row number).
    Row 1 is the header; data starts at row 2.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="Slots!A:Z",
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return [], {}

    # rows[0] = header row; data rows start at rows[1]
    id_to_row_number: dict[str, int] = {}
    for i, row in enumerate(rows[1:], start=2):   # 2 = first data row in Sheets (1-indexed)
        if row:
            id_to_row_number[row[0]] = i

    return rows, id_to_row_number


def expire_old_slots(service, sheet_id: str, existing_rows: list, id_to_row: dict[str, int]) -> int:
    """
    Find slots in the sheet that are no longer in our 72h window and mark them expired.
    Returns count of expired rows.
    """
    from normalize_slot import compute_hours_until

    updates = []
    expired = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in existing_rows[1:]:   # skip header
        if not row:
            continue
        slot_id = row[0] if row else ""
        status  = row[STATUS_COL] if len(row) > STATUS_COL else ""

        if status in ("expired", "booked"):
            continue

        # Re-check the start_time from the sheet
        start_time_col = SLOTS_COLUMNS.index("start_time")
        start_time = row[start_time_col] if len(row) > start_time_col else ""
        hours = compute_hours_until(start_time)

        if hours is not None and hours <= 0:
            row_number = id_to_row.get(slot_id)
            if row_number:
                updates.append({
                    "range": f"Slots!Y{row_number}:Z{row_number}",  # status + last_updated
                    "values": [["expired", now_iso]],
                })
                expired += 1

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    return expired


def upsert_slots(service, sheet_id: str, slots: list[dict], id_to_row: dict[str, int]) -> tuple[int, int]:
    """
    Upsert slots into the Slots tab.
    Returns (new_count, updated_count).
    """
    updates  = []   # existing rows to update
    new_rows = []   # new rows to append

    for slot in slots:
        slot_id = slot.get("slot_id", "")
        row     = slot_to_row(slot)

        if slot_id in id_to_row:
            row_number = id_to_row[slot_id]
            updates.append({
                "range":  f"Slots!A{row_number}",
                "values": [row],
            })
        else:
            new_rows.append(row)

    # Batch update existing rows
    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    # Append new rows
    if new_rows:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Slots!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

    return len(new_rows), len(updates)


def append_run_log(service, sheet_id: str, run_stats: dict) -> None:
    """Append a single row to the RunLog tab."""
    run_log_columns = [
        "run_id", "started_at", "finished_at", "duration_seconds",
        "slots_fetched", "slots_new", "slots_updated", "slots_expired",
        "platforms_succeeded", "platforms_failed",
        "posts_twitter", "posts_reddit", "posts_telegram",
        "errors",
    ]
    row = [str(run_stats.get(col, "")) for col in run_log_columns]
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="RunLog!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="Upsert slots to Google Sheets")
    parser.add_argument("--data-file", default=str(DATA_FILE))
    parser.add_argument("--sheet-id", default=os.getenv("GOOGLE_SHEET_ID"))
    parser.add_argument("--run-id", default=str(uuid.uuid4())[:8])
    parser.add_argument("--started-at", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()

    if not GOOGLE_LIBS_AVAILABLE:
        print("Google Sheets libraries not installed — skipping. Run: pip install google-auth google-auth-oauthlib google-api-python-client")
        return

    if not args.sheet_id:
        print("Google Sheets not configured (GOOGLE_SHEET_ID missing) — skipping.")
        return

    if not Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).exists() and not Path("token.json").exists():
        print("Google OAuth not configured (credentials.json missing) — skipping.")
        print("Run: python tools/setup_google_sheets.py after adding credentials.json")
        return

    data_path = Path(args.data_file)
    if not data_path.exists():
        print(f"ERROR: {data_path} not found.")
        sys.exit(1)

    slots = json.loads(data_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(slots)} slots from {data_path}")

    print("Authenticating with Google Sheets...")
    creds   = get_credentials()
    service = build("sheets", "v4", credentials=creds)

    print("Fetching existing rows...")
    try:
        existing_rows, id_to_row = fetch_existing_rows(service, args.sheet_id)
        print(f"  {len(id_to_row)} existing slots in sheet")
    except HttpError as e:
        print(f"ERROR reading sheet: {e}")
        sys.exit(1)

    print("Expiring stale slots...")
    expired = expire_old_slots(service, args.sheet_id, existing_rows, id_to_row)
    print(f"  {expired} slots marked expired")

    print("Upserting slots...")
    try:
        new_count, updated_count = upsert_slots(service, args.sheet_id, slots, id_to_row)
    except HttpError as e:
        print(f"ERROR writing to sheet: {e}")
        sys.exit(1)

    finished_at = datetime.now(timezone.utc).isoformat()

    run_stats = {
        "run_id":        args.run_id,
        "started_at":    args.started_at,
        "finished_at":   finished_at,
        "slots_fetched": len(slots),
        "slots_new":     new_count,
        "slots_updated": updated_count,
        "slots_expired": expired,
    }
    append_run_log(service, args.sheet_id, run_stats)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{args.sheet_id}"
    print(f"\nSheets write complete")
    print(f"  New     : {new_count}")
    print(f"  Updated : {updated_count}")
    print(f"  Expired : {expired}")
    print(f"  View    : {sheet_url}")


if __name__ == "__main__":
    main()
