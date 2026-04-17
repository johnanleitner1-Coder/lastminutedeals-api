"""
check_api_health.py — Lightweight connectivity check for all configured APIs.

Runs a minimal test call against each platform API to confirm credentials work
and the endpoint is reachable before a full pipeline run.

Usage:
    python tools/check_api_health.py

Exits with code 0 if all configured APIs pass, 1 if any fail.
"""

import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

RESULTS: list[tuple[str, str, str]] = []   # (name, status, detail)


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "OK  " if ok else "FAIL"
    RESULTS.append((name, status, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def check_netlify() -> None:
    token   = os.getenv("NETLIFY_AUTH_TOKEN", "").strip()
    site_id = os.getenv("NETLIFY_SITE_ID", "").strip()
    if not token or not site_id:
        check("Netlify", False, "NETLIFY_AUTH_TOKEN or NETLIFY_SITE_ID not set")
        return
    try:
        r = requests.get(
            f"https://api.netlify.com/api/v1/sites/{site_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = r.json()
        if r.status_code == 200:
            url = data.get("url") or data.get("ssl_url", "?")
            check("Netlify", True, f"Site reachable — {url}")
        else:
            check("Netlify", False, f"HTTP {r.status_code}: {data.get('message', r.text[:60])}")
    except Exception as e:
        check("Netlify", False, str(e))


def check_telegram() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        check("Telegram", False, "TELEGRAM_BOT_TOKEN not set")
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            username = data["result"].get("username", "?")
            check("Telegram", True, f"Bot @{username} active")
        else:
            check("Telegram", False, data.get("description", r.text[:60]))
    except Exception as e:
        check("Telegram", False, str(e))


def check_twitter() -> None:
    key    = os.getenv("TWITTER_API_KEY", "").strip()
    secret = os.getenv("TWITTER_API_SECRET", "").strip()
    if not key or not secret:
        check("Twitter/X", False, "TWITTER_API_KEY or TWITTER_API_SECRET not set")
        return
    # Use app-only bearer token (OAuth 2.0 client credentials) for health check
    try:
        r = requests.post(
            "https://api.twitter.com/oauth2/token",
            auth=(key, secret),
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        data = r.json()
        if r.status_code == 200 and data.get("token_type") == "bearer":
            check("Twitter/X", True, "App credentials valid")
        else:
            check("Twitter/X", False, f"HTTP {r.status_code}: {data.get('errors', r.text[:60])}")
    except Exception as e:
        check("Twitter/X", False, str(e))


def check_google_sheets() -> None:
    sheet_id   = os.getenv("GOOGLE_SHEET_ID", "").strip()
    creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    token_path = "token.json"
    if not sheet_id:
        check("Google Sheets", False, "GOOGLE_SHEET_ID not set")
        return
    if not Path(creds_path).exists() and not Path(token_path).exists():
        check("Google Sheets", False, f"credentials.json missing — run setup_google_sheets.py")
        return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        if not Path(token_path).exists():
            check("Google Sheets", False, "token.json missing — OAuth not completed yet")
            return
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(token_path)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(token_path).write_text(creds.to_json())
        service = build("sheets", "v4", credentials=creds)
        result  = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        title   = result.get("properties", {}).get("title", "?")
        check("Google Sheets", True, f'Sheet "{title}" accessible')
    except ImportError:
        check("Google Sheets", False, "google-api-python-client not installed")
    except Exception as e:
        check("Google Sheets", False, str(e)[:80])


def main():
    print("API Health Check\n" + "=" * 40)

    check_netlify()
    check_telegram()
    check_twitter()
    check_google_sheets()

    print("\n" + "=" * 40)
    failures = [r for r in RESULTS if r[1] == "FAIL"]
    configured = [r for r in RESULTS if r[1] == "OK  "]
    print(f"{len(configured)}/{len(RESULTS)} services healthy")

    if failures:
        print("\nNot configured or failing:")
        for name, _, detail in failures:
            print(f"  • {name}: {detail}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
