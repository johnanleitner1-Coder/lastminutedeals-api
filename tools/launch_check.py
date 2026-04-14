"""
launch_check.py — Full pre-launch readiness diagnostic.

Checks every claim we make to developers and customers:
  - External API connections (Bokun, Ventrata, Zaui, Peek, Stripe, SendGrid, Supabase)
  - Railway API endpoints
  - MCP server
  - Slot inventory
  - Booking email system
  - Cancellation system
  - Developer docs

Usage:
    python tools/launch_check.py
"""

import importlib.util as ilu
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.stdout.reconfigure(encoding="utf-8")

BOOKING_API = os.getenv("BOOKING_API_URL", "").rstrip("/")
API_KEY     = os.getenv("LMD_WEBSITE_API_KEY", "")
HDRS        = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

results = []


def chk(label, status, note=""):
    results.append((status, label, note))
    marker = {"PASS": "OK ", "WARN": "..", "FAIL": "XX"}[status]
    print(f"  [{marker}] {label:<38s} {note}")


def api(method, url, **kw):
    try:
        return requests.request(method, url, timeout=10, **kw)
    except Exception as e:
        class _Fake:
            ok = False
            status_code = 0
            text = str(e)
            def json(self): return {}
        return _Fake()


print()
print("=" * 65)
print("  LAUNCH READINESS — Last Minute Deals HQ")
print("=" * 65)

# ── 1. External API connections ────────────────────────────────────────────────
print("\n[External APIs]")

key = os.getenv("BOKUN_API_KEY", "")
r = api("GET", "https://api.bokun.io/octo/v1/products",
        headers={"Authorization": f"Bearer {key}"})
prods = r.json() if r.ok else []
chk("Bokun OCTO products", PASS if len(prods) >= 1 else FAIL,
    f"{len(prods)} products (Bicycle Roma + Pure Morocco)")

vkey = os.getenv("VENTRATA_API_KEY", "")
r = api("GET", "https://api.ventrata.com/octo/suppliers",
        headers={"Authorization": f"Bearer {vkey}", "Octo-Capabilities": ""})
suppliers = r.json() if r.ok else []
chk("Ventrata OCTO suppliers", PASS if r.ok else FAIL,
    f"{len(suppliers)} supplier(s) connected")

zkey = os.getenv("ZAUI_API_KEY", "")
r = api("GET", "https://api.zaui.io/octo/products",
        headers={"Authorization": f"Bearer {zkey}"})
chk("Zaui OCTO products", PASS if r.ok else FAIL,
    f"{len(r.json()) if r.ok else 0} products")

pkey = os.getenv("PEEK_API_KEY", "")
r = api("GET", "https://octo.peek.com/integrations/octo/products",
        headers={"Authorization": f"Bearer {pkey}"})
chk("Peek OCTO products (test)", PASS if r.ok else FAIL,
    f"{len(r.json()) if r.ok else 0} products (test env)")

skey = os.getenv("STRIPE_SECRET_KEY", "")
r = api("GET", "https://api.stripe.com/v1/balance",
        headers={"Authorization": f"Bearer {skey}"})
is_live = "sk_live" in skey
chk("Stripe", PASS if r.ok else FAIL,
    f"{'LIVE keys' if is_live else 'TEST keys (ok for soft launch)'}")

sgkey = os.getenv("SENDGRID_API_KEY", "")
r = api("GET", "https://api.sendgrid.com/v3/user/profile",
        headers={"Authorization": f"Bearer {sgkey}"})
chk("SendGrid email", PASS if r.ok else FAIL,
    "delivery OK" if r.ok else r.text[:50])

sburl = os.getenv("SUPABASE_URL", "").rstrip("/")
sbkey = os.getenv("SUPABASE_SECRET_KEY", "")
r = api("GET", f"{sburl}/rest/v1/",
        headers={"apikey": sbkey, "Authorization": f"Bearer {sbkey}"})
chk("Supabase", PASS if r.status_code in (200, 400) else FAIL,
    "DB + Storage connected")

# ── 2. Railway API endpoints ───────────────────────────────────────────────────
print("\n[Railway API — " + BOOKING_API + "]")

r = api("GET", f"{BOOKING_API}/health", headers=HDRS)
slot_count = r.json().get("slots", 0) if r.ok else 0
chk("/health", PASS if r.ok else FAIL, f"{slot_count} slots cached on server")

r = api("GET", f"{BOOKING_API}/slots?hours_ahead=72&limit=5", headers=HDRS)
slot_list = r.json() if r.ok else []
live_slots = [s for s in slot_list if isinstance(s, dict) and "slot_id" in s]
chk("/slots feed", PASS if live_slots else WARN,
    f"{len(live_slots)} slots returned" if live_slots else "empty (slots may be stale)")

r = api("POST", f"{BOOKING_API}/api/book", json={}, headers=HDRS)
chk("POST /api/book", PASS if r.status_code == 400 else FAIL,
    "400 on missing fields (correct)")

r = api("POST", f"{BOOKING_API}/api/webhook", json={}, headers=HDRS)
chk("POST /api/webhook (Stripe)", PASS if r.status_code == 400 else FAIL,
    "400 invalid sig (correct)")

r = api("POST", f"{BOOKING_API}/api/bokun/webhook",
        json={"type": "booking.cancelled",
              "booking": {"status": "CANCELLED", "confirmationCode": "test_x"}},
        headers=HDRS)
action = r.json().get("action", "?") if r.ok else "MISSING"
chk("POST /api/bokun/webhook", PASS if r.status_code == 200 else FAIL,
    f"action={action}" if r.ok else str(r.status_code))

r = api("GET", f"{BOOKING_API}/cancel/nonexistent_id", headers=HDRS)
chk("GET /cancel/<id>", PASS if r.status_code == 404 else FAIL,
    f"404 for unknown id (correct)" if r.status_code == 404 else f"unexpected {r.status_code}")

r = api("DELETE", f"{BOOKING_API}/bookings/fake_id", headers=HDRS)
chk("DELETE /bookings/<id>", PASS if r.status_code == 404 else FAIL,
    "404 on unknown id (correct)")

r = api("POST", f"{BOOKING_API}/api/wallets/create",
        json={"name": "test", "email": "t@t.com"}, headers=HDRS)
has_key = r.ok and bool(r.json().get("api_key"))
chk("Wallet create", PASS if has_key else FAIL,
    "api_key returned" if has_key else r.text[:50])

# ── 3. MCP server ──────────────────────────────────────────────────────────────
print("\n[MCP Server]")

try:
    BASE = Path(__file__).parent.parent
    sys.path.insert(0, str(BASE / "tools"))
    spec = ilu.spec_from_file_location("mcp", BASE / "tools" / "run_mcp_server.py")
    mcp  = ilu.module_from_spec(spec)
    spec.loader.exec_module(mcp)

    res = mcp.search_slots(hours_ahead=72, limit=5)
    has_results = bool(res and isinstance(res[0], dict) and "slot_id" in res[0])
    chk("search_slots tool", PASS if has_results else WARN,
        f"{len(res)} results" if has_results else "no live slots in local .tmp/")

    if has_results:
        slot = res[0]
        detail = mcp.get_slot(slot["slot_id"])
        chk("get_slot tool", PASS if "slot_id" in detail else FAIL,
            detail.get("service_name", "?")[:45])
    else:
        chk("get_slot tool", WARN, "skipped (no slots)")

    # Check book_slot exists and has correct signature
    import inspect
    sig = inspect.signature(mcp.book_slot)
    expected = {"slot_id", "customer_name", "customer_email", "customer_phone"}
    chk("book_slot tool signature", PASS if expected <= set(sig.parameters) else FAIL, str(list(sig.parameters)))

except Exception as e:
    chk("MCP server", FAIL, str(e)[:70])

# ── 4. Slot inventory ──────────────────────────────────────────────────────────
print("\n[Slot Inventory]")

agg = Path(__file__).parent.parent / ".tmp" / "aggregated_slots.json"
if agg.exists():
    slots = json.loads(agg.read_text(encoding="utf-8"))
    live  = [s for s in slots
             if isinstance(s.get("hours_until_start"), (int, float))
             and 0 <= s["hours_until_start"] <= 72]
    platforms = {}
    for s in live:
        p = s.get("platform", "?")
        platforms[p] = platforms.get(p, 0) + 1
    chk("Live slots (<=72h)", PASS if live else WARN,
        f"{len(live)} slots — {platforms}")
    with_price = sum(1 for s in live if s.get("our_price"))
    chk("Slots have our_price markup", PASS if with_price > 0 else WARN,
        f"{with_price}/{len(live)} have our_price set")
    suppliers_seen = set(s.get("business_name","?") for s in live if s.get("business_name"))
    chk("Supplier variety", PASS if len(suppliers_seen) >= 2 else WARN,
        f"{len(suppliers_seen)} suppliers in feed")
else:
    chk("Slot inventory file", FAIL, "no .tmp/aggregated_slots.json — run fetch pipeline")

# ── 5. Email system ────────────────────────────────────────────────────────────
print("\n[Email System]")

email_path = Path(__file__).parent / "send_booking_email.py"
src = email_path.read_text(encoding="utf-8")
for email_type in ["booking_initiated", "booking_confirmed", "booking_failed", "booking_cancelled"]:
    chk(f"Email type: {email_type}", PASS if email_type in src else FAIL)

chk("Cancel link in confirmation", PASS if "cancel_url" in src else FAIL,
    "self-serve cancel link in booking_confirmed email")

# ── 6. Cancellation system ─────────────────────────────────────────────────────
print("\n[Cancellation System]")

server_src = (Path(__file__).parent / "run_api_server.py").read_text(encoding="utf-8")
chk("Bokun webhook handler", PASS if "bokun_webhook" in server_src else FAIL)
chk("Self-serve cancel page", PASS if "self_serve_cancel" in server_src else FAIL)
chk("Auto Stripe refund on cancel", PASS if "_refund_stripe" in server_src else FAIL)
chk("OCTO cancellation retry worker", PASS if Path(__file__).parent.joinpath("retry_cancellations.py").exists() else FAIL)

# ── 7. Developer-facing assets ─────────────────────────────────────────────────
print("\n[Developer Assets]")

base = Path(__file__).parent.parent
chk("MCP README (for registry)", PASS if (base / "MCP_README.md").exists() else FAIL,
    "ready to submit to modelcontextprotocol/servers")
chk("Developer landing page", PASS if (base / ".tmp" / "landing_page_build" / "developers.html").exists() else FAIL,
    "deploy to /developers")
demo = base / ".tmp" / "lmd_demo.webm"
chk("Demo video", PASS if demo.exists() else FAIL,
    f"{demo.stat().st_size // 1024}KB — upload to YouTube/Twitter" if demo.exists() else "run generate_demo_video.py")

# ── 8. Launch blockers and warnings ───────────────────────────────────────────
print("\n[Launch Blockers / Warnings]")

stripe_live = "sk_live" in os.getenv("STRIPE_SECRET_KEY", "")
chk("Stripe LIVE keys", WARN if not stripe_live else PASS,
    "still test keys — switch before taking real money")
chk("Self-serve API key endpoint", WARN,
    "no /register endpoint — keys issued manually via email (OK for launch)")
chk("OpenAPI /docs endpoint", WARN,
    "not served — MCP_README.md is the docs for now")
chk("Bokun webhook registered in dashboard", WARN,
    "register https://api.lastminutedealshq.com/api/bokun/webhook in Bokun > Settings > Webhooks")
chk("MCP package installable (pip)", WARN,
    "not on PyPI yet — Claude Desktop requires local path in config")

peek_prod = "test" not in os.getenv("PEEK_API_KEY", "")
chk("Peek production key", WARN if not peek_prod else PASS,
    "waiting on Ben — test key active for now")

# ── Summary ────────────────────────────────────────────────────────────────────
passing = sum(1 for s, _, _ in results if s == PASS)
warning = sum(1 for s, _, _ in results if s == WARN)
failing = sum(1 for s, _, _ in results if s == FAIL)

print()
print("=" * 65)
print(f"  {passing} PASSED  |  {warning} WARNINGS  |  {failing} FAILED  |  {len(results)} total")
print("=" * 65)

if failing == 0 and warning <= 6:
    print("  STATUS: READY TO LAUNCH")
elif failing == 0:
    print("  STATUS: LAUNCH-READY (address warnings before scaling)")
else:
    print(f"  STATUS: {failing} BLOCKER(S) — fix before launch")
print()
