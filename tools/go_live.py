"""
go_live.py — Switch Last Minute Deals from test to production.

Requires:
  1. Stripe LIVE keys from dashboard.stripe.com (toggle Live mode top-right)
  2. Railway API token from railway.app/account/tokens

What this script does:
  - Updates STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET
    in Railway environment via Railway API
  - Updates local .env for local testing
  - Verifies the Stripe live connection
  - Prints webhook registration instructions

Usage:
    python tools/go_live.py \
        --stripe-secret sk_live_... \
        --stripe-publishable pk_live_... \
        --stripe-webhook whsec_... \
        --railway-token <your Railway API token>

Get Railway token: railway.app → Account → Tokens → Create token
Get Stripe keys:   dashboard.stripe.com → toggle Live → Developers → API Keys
Get Stripe webhook: dashboard.stripe.com → Developers → Webhooks → Add endpoint
    URL: https://api.lastminutedealshq.com/api/webhook
    Events: payment_intent.succeeded, payment_intent.payment_failed,
            checkout.session.completed, checkout.session.expired
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

sys.stdout.reconfigure(encoding="utf-8")

RAILWAY_GRAPHQL = "https://backboard.railway.app/graphql/v2"


def update_railway_var(token: str, project_id: str, service_id: str,
                       environment_id: str, name: str, value: str) -> bool:
    """Set a single Railway environment variable via GraphQL."""
    query = """
    mutation UpsertVariables($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId":     project_id,
            "serviceId":     service_id,
            "environmentId": environment_id,
            "variables":     {name: value},
        }
    }
    r = requests.post(
        RAILWAY_GRAPHQL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=15,
    )
    data = r.json()
    if "errors" in data:
        print(f"  ERROR setting {name}: {data['errors'][0]['message']}")
        return False
    return True


def get_railway_project_info(token: str) -> tuple[str, str, str]:
    """Return (project_id, service_id, environment_id) for the production deployment."""
    query = """
    query {
      me {
        projects {
          edges {
            node {
              id
              name
              services {
                edges {
                  node {
                    id
                    name
                  }
                }
              }
              environments {
                edges {
                  node {
                    id
                    name
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    r = requests.post(
        RAILWAY_GRAPHQL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query},
        timeout=15,
    )
    data = r.json()
    if "errors" in data:
        raise ValueError(f"Railway API error: {data['errors'][0]['message']}")

    projects = data["data"]["me"]["projects"]["edges"]
    if not projects:
        raise ValueError("No Railway projects found for this token")

    # Find the lastminutedeals project
    project = None
    for p in projects:
        n = p["node"]
        if "lastminute" in n["name"].lower() or "lmd" in n["name"].lower() or "deals" in n["name"].lower():
            project = n
            break
    if not project:
        project = projects[0]["node"]  # fallback to first project

    project_id = project["id"]
    services   = project["services"]["edges"]
    envs       = project["environments"]["edges"]

    service_id = services[0]["node"]["id"] if services else ""
    # Find production environment
    env_id = ""
    for e in envs:
        if "prod" in e["node"]["name"].lower() or e["node"]["name"] == "production":
            env_id = e["node"]["id"]
            break
    if not env_id and envs:
        env_id = envs[0]["node"]["id"]

    print(f"  Project:     {project['name']} ({project_id[:16]}...)")
    print(f"  Service:     {services[0]['node']['name'] if services else 'none'} ({service_id[:16]}...)")
    print(f"  Environment: {envs[0]['node']['name'] if envs else 'none'} ({env_id[:16]}...)")
    return project_id, service_id, env_id


def verify_stripe_key(secret_key: str) -> bool:
    """Verify Stripe key is valid and is a live key."""
    r = requests.get(
        "https://api.stripe.com/v1/balance",
        headers={"Authorization": f"Bearer {secret_key}"},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  ERROR: Stripe key invalid — {r.json().get('error',{}).get('message','')}")
        return False
    if "sk_test" in secret_key:
        print("  WARNING: This is a TEST key, not a live key")
        return False
    return True


def update_env_file(updates: dict) -> None:
    """Update .env file with new key=value pairs."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = set()
    new_lines = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated.add(key)
                continue
        new_lines.append(line)
    # Append any keys not found in existing file
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Switch LMD to production")
    parser.add_argument("--stripe-secret",      required=True, help="sk_live_... from Stripe dashboard")
    parser.add_argument("--stripe-publishable",  required=True, help="pk_live_... from Stripe dashboard")
    parser.add_argument("--stripe-webhook",      required=True, help="whsec_... from Stripe webhook config")
    parser.add_argument("--railway-token",       required=False, default="",
                        help="Railway API token (railway.app/account/tokens)")
    parser.add_argument("--local-only",          action="store_true",
                        help="Only update local .env, skip Railway")
    args = parser.parse_args()

    print("\n=== Last Minute Deals — Go Live ===\n")

    # 1. Verify Stripe key
    print("[1] Verifying Stripe live key...")
    if not verify_stripe_key(args.stripe_secret):
        if "sk_test" not in args.stripe_secret:
            sys.exit(1)
        print("  Continuing with test key (use --stripe-secret sk_live_... for production)")
    else:
        print("  Stripe live key: valid")

    stripe_updates = {
        "STRIPE_SECRET_KEY":      args.stripe_secret,
        "STRIPE_PUBLISHABLE_KEY": args.stripe_publishable,
        "STRIPE_WEBHOOK_SECRET":  args.stripe_webhook,
    }

    # 2. Update local .env
    print("\n[2] Updating local .env...")
    update_env_file(stripe_updates)
    print("  .env updated")

    # 3. Update Railway
    if not args.local_only and args.railway_token:
        print("\n[3] Updating Railway environment variables...")
        try:
            project_id, service_id, env_id = get_railway_project_info(args.railway_token)
            all_ok = True
            for name, value in stripe_updates.items():
                ok = update_railway_var(args.railway_token, project_id, service_id, env_id, name, value)
                status = "OK" if ok else "FAIL"
                print(f"  [{status}] {name}")
                if not ok:
                    all_ok = False
            if all_ok:
                print("\n  Railway updated. Redeploy will happen automatically.")
            else:
                print("\n  Some Railway updates failed. Set manually in Railway dashboard.")
        except Exception as e:
            print(f"  Railway API error: {e}")
            print("  Set these manually in Railway Dashboard -> Variables:")
            for k, v in stripe_updates.items():
                print(f"    {k} = {v[:20]}...")
    elif not args.local_only:
        print("\n[3] Railway update skipped (no --railway-token)")
        print("  Set these manually in Railway Dashboard -> Variables:")
        for k, v in stripe_updates.items():
            print(f"    {k} = {v[:20]}...")

    # 4. Stripe webhook instructions
    print("\n[4] Stripe webhook setup")
    print("  If you haven't already, create a live webhook in Stripe:")
    print("  dashboard.stripe.com -> Developers -> Webhooks -> Add endpoint")
    print("  URL: https://api.lastminutedealshq.com/api/webhook")
    print("  Events to select:")
    print("    - payment_intent.succeeded")
    print("    - payment_intent.payment_failed")
    print("    - checkout.session.completed")
    print("    - checkout.session.expired")
    print("  Then copy the webhook signing secret (whsec_...) and re-run with --stripe-webhook")

    print("\n=== Done ===")
    print("Run: python tools/launch_check.py  to verify everything is green\n")


if __name__ == "__main__":
    main()
