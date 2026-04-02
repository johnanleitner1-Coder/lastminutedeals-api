"""
setup_landing_page.py — One-time Netlify site creation.

Creates a new Netlify site and saves the Site ID to .env.
Run once, then deploy_landing_page.py handles all future deploys.

Prerequisites:
  1. Create a free Netlify account at https://netlify.com
  2. Go to: User Settings → Applications → Personal access tokens → New access token
  3. Add to .env:  NETLIFY_AUTH_TOKEN=<your token>

Usage:
    python tools/setup_landing_page.py [--site-name last-minute-deals]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Create Netlify site (run once)")
    parser.add_argument("--site-name", default="last-minute-deals",
                        help="Netlify subdomain (e.g. last-minute-deals -> last-minute-deals.netlify.app)")
    args = parser.parse_args()

    token = os.getenv("NETLIFY_AUTH_TOKEN", "").strip()
    if not token:
        print("ERROR: NETLIFY_AUTH_TOKEN not set in .env")
        print()
        print("Steps to get your token:")
        print("  1. Sign up free at https://app.netlify.com/signup")
        print("  2. Go to: avatar -> User settings -> Applications -> Personal access tokens")
        print("  3. Click 'New access token', name it 'LastMinuteDeals', copy the token")
        print("  4. Add to .env:  NETLIFY_AUTH_TOKEN=<token>")
        print("  5. Re-run this script")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check if site already exists
    existing_id = os.getenv("NETLIFY_SITE_ID", "").strip()
    if existing_id:
        print(f"NETLIFY_SITE_ID already set: {existing_id}")
        print("Site already configured. Run update_landing_page.py to deploy.")
        return

    # Create a new Netlify site
    print(f"Creating Netlify site '{args.site_name}'...")
    resp = requests.post(
        "https://api.netlify.com/api/v1/sites",
        headers=headers,
        json={"name": args.site_name},
        timeout=15,
    )

    if resp.status_code in (200, 201):
        site = resp.json()
        site_id  = site["id"]
        site_url = site.get("ssl_url") or site.get("url") or f"https://{args.site_name}.netlify.app"

        print(f"\nSite created!")
        print(f"  Site ID  : {site_id}")
        print(f"  Site URL : {site_url}")

        # Write to .env
        env_path = Path(".env")
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            content = content.replace("NETLIFY_SITE_ID=", f"NETLIFY_SITE_ID={site_id}")
            content = content.replace("LANDING_PAGE_URL=", f"LANDING_PAGE_URL={site_url}")
            env_path.write_text(content, encoding="utf-8")
            print(f"\nSaved to .env:")
            print(f"  NETLIFY_SITE_ID={site_id}")
            print(f"  LANDING_PAGE_URL={site_url}")

        print(f"\nNext: run  python tools/update_landing_page.py  to deploy your first page.")

    elif resp.status_code == 422:
        # Name taken — try with a suffix
        import random
        suffix = random.randint(100, 999)
        new_name = f"{args.site_name}-{suffix}"
        print(f"Name '{args.site_name}' taken, trying '{new_name}'...")
        resp2 = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers=headers,
            json={"name": new_name},
            timeout=15,
        )
        if resp2.status_code in (200, 201):
            site = resp2.json()
            site_id  = site["id"]
            site_url = site.get("ssl_url") or site.get("url")
            print(f"Site created: {site_url}")

            env_path = Path(".env")
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                content = content.replace("NETLIFY_SITE_ID=", f"NETLIFY_SITE_ID={site_id}")
                content = content.replace("LANDING_PAGE_URL=", f"LANDING_PAGE_URL={site_url}")
                env_path.write_text(content, encoding="utf-8")
        else:
            print(f"ERROR: {resp2.status_code} {resp2.text[:200]}")
            sys.exit(1)
    else:
        print(f"ERROR: {resp.status_code}")
        print(resp.text[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
