"""
setup_cloudflare_pages.py — One-time Cloudflare Pages project setup.

Creates the Pages project, deploys the current landing page, and writes
CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_PAGES_PROJECT to .env.

Run this once after getting a Cloudflare API token:
  python tools/setup_cloudflare_pages.py --token YOUR_TOKEN

How to get your token + account ID:
  1. Go to dash.cloudflare.com
  2. Top-right menu -> My Profile -> API Tokens -> Create Token
  3. Use template "Edit Cloudflare Workers" OR create custom token with:
       - Cloudflare Pages: Edit  (Account resource: your account)
  4. Copy the token
  5. Account ID: dash.cloudflare.com -> right sidebar (any zone page) OR
     dash.cloudflare.com/profile/api-tokens -> account ID shown below your email

Usage:
  python tools/setup_cloudflare_pages.py --token <token> [--account-id <id>] [--project lastminutedeals]
"""

import argparse
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

ENV_FILE  = Path(".env")
HTML_FILE = Path(".tmp/site/index.html")
CF_API    = "https://api.cloudflare.com/client/v4"


def cf_get(token: str, path: str) -> dict:
    r = requests.get(f"{CF_API}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    return r.json()


def cf_post(token: str, path: str, json_data: dict) -> dict:
    r = requests.post(
        f"{CF_API}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=json_data,
        timeout=30,
    )
    return r.json()


def cf_post_multipart(token: str, path: str, files: dict) -> dict:
    r = requests.post(
        f"{CF_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        files=files,
        timeout=60,
    )
    return r.json()


def get_account_id(token: str) -> str | None:
    """Auto-detect account ID from the token's accessible accounts."""
    resp = cf_get(token, "/accounts?per_page=5")
    accounts = (resp.get("result") or [])
    if not accounts:
        return None
    if len(accounts) == 1:
        return accounts[0]["id"]
    print("Multiple accounts found:")
    for i, a in enumerate(accounts):
        print(f"  [{i}] {a['name']} ({a['id']})")
    idx = input("Enter number to use [0]: ").strip() or "0"
    return accounts[int(idx)]["id"]


def create_pages_project(token: str, account_id: str, project_name: str) -> dict:
    """Create a new Cloudflare Pages project (direct upload type)."""
    resp = cf_post(token, f"/accounts/{account_id}/pages/projects", {
        "name":              project_name,
        "production_branch": "main",
    })
    return resp


def deploy_html(token: str, account_id: str, project_name: str, html_path: Path) -> dict:
    """Deploy the HTML file to the Pages project using manifest-based upload."""
    import hashlib, json as _json
    html_bytes    = html_path.read_bytes()
    hdr_bytes     = b"/*\n  Content-Type: text/html; charset=UTF-8\n"
    html_hash     = hashlib.sha256(html_bytes).hexdigest()
    hdr_hash      = hashlib.sha256(hdr_bytes).hexdigest()
    manifest      = {"/index.html": html_hash, "/_headers": hdr_hash}
    files = {
        "manifest":               (None,          _json.dumps(manifest), "application/json"),
        f"files[{html_hash}]":    ("index.html",  html_bytes,            "text/html"),
        f"files[{hdr_hash}]":     ("_headers",    hdr_bytes,             "text/plain"),
    }
    return cf_post_multipart(
        token,
        f"/accounts/{account_id}/pages/projects/{project_name}/deployments",
        files,
    )


def write_env(key: str, value: str) -> None:
    """Write or update a key in .env."""
    if ENV_FILE.exists():
        content = ENV_FILE.read_text(encoding="utf-8")
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content += f"\n{key}={value}\n"
        ENV_FILE.write_text(content, encoding="utf-8")
    else:
        ENV_FILE.write_text(f"{key}={value}\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="One-time Cloudflare Pages setup")
    parser.add_argument("--token",      required=True, help="Cloudflare API token")
    parser.add_argument("--account-id", default="",    help="Cloudflare Account ID (auto-detected if omitted)")
    parser.add_argument("--project",    default="lastminutedeals", help="Pages project name (default: lastminutedeals)")
    args = parser.parse_args()

    token   = args.token.strip()
    project = args.project.strip()

    # ── 1. Verify token ────────────────────────────────────────────────────────
    print("Verifying API token...")
    verify = cf_get(token, "/user/tokens/verify")
    if not verify.get("success"):
        print(f"Token invalid: {verify.get('errors')}")
        sys.exit(1)
    print(f"  Token OK: {verify.get('result', {}).get('status', 'active')}")

    # ── 2. Get account ID ─────────────────────────────────────────────────────
    account_id = args.account_id.strip()
    if not account_id:
        print("Detecting account ID...")
        account_id = get_account_id(token)
        if not account_id:
            print("Could not detect account ID. Pass --account-id manually.")
            print("Find it at: dash.cloudflare.com -> right sidebar")
            sys.exit(1)
    print(f"  Account ID: {account_id}")

    # ── 3. Create or verify Pages project ────────────────────────────────────
    print(f"Setting up Pages project '{project}'...")

    # Check if project already exists
    existing = cf_get(token, f"/accounts/{account_id}/pages/projects/{project}")
    if existing.get("success"):
        print(f"  Project already exists: {existing['result']['subdomain']}.pages.dev")
    else:
        result = create_pages_project(token, account_id, project)
        if result.get("success"):
            subdomain = result["result"].get("subdomain", f"{project}.pages.dev")
            print(f"  Project created: {subdomain}")
        else:
            errors = result.get("errors", result)
            # Code 8000007 = project name taken — try with suffix
            if any(e.get("code") == 8000007 for e in (errors or [])):
                print(f"  Name '{project}' taken — try --project {project}-deals or similar")
            else:
                print(f"  Create failed: {errors}")
            sys.exit(1)

    # ── 4. Write .env ─────────────────────────────────────────────────────────
    write_env("CLOUDFLARE_API_TOKEN",       token)
    write_env("CLOUDFLARE_ACCOUNT_ID",      account_id)
    write_env("CLOUDFLARE_PAGES_PROJECT",   project)

    # Build landing page URL
    check = cf_get(token, f"/accounts/{account_id}/pages/projects/{project}")
    subdomain = (check.get("result") or {}).get("subdomain", f"{project}.pages.dev")
    cf_url    = f"https://{subdomain}"
    write_env("LANDING_PAGE_URL", cf_url)

    print(f"  .env updated: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_PAGES_PROJECT")
    print(f"  LANDING_PAGE_URL set to: {cf_url}")

    # ── 5. Deploy current landing page ────────────────────────────────────────
    if HTML_FILE.exists():
        print(f"Deploying current landing page...")
        deploy = deploy_html(token, account_id, project, HTML_FILE)
        if deploy.get("success"):
            deploy_url = (deploy.get("result") or {}).get("url", cf_url)
            print(f"  Live at: {deploy_url}")
        else:
            print(f"  Deploy failed: {deploy.get('errors')}")
            print(f"  Try manually: python tools/update_landing_page.py")
    else:
        print(f"  No HTML at {HTML_FILE} yet — run: python tools/update_landing_page.py")

    print()
    print("Setup complete. Cloudflare Pages is now your deployment target.")
    print("Netlify will no longer be used. All future pipeline runs deploy to Cloudflare.")
    print(f"Live URL: {cf_url}")


if __name__ == "__main__":
    main()
