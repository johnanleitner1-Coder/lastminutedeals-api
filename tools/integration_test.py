"""
integration_test.py — Full-surface smoke test for the LastMinuteDeals system.

Run this after every autonomous fix attempt to verify no regressions were introduced.
All checks are READ-ONLY and NON-DESTRUCTIVE — no real bookings, no Stripe charges,
no database writes.

Usage:
    python tools/integration_test.py [--blocking-only] [--json]

Sections:
  A. Core Python pipeline (slot schema, aggregation, pricing, normalize)
  B. MCP server (soft check — skipped if server not running)
  C. Supabase (slots table, bookings, wallets, Storage buckets)
  D. External APIs and credentials (Stripe, SendGrid, Twilio, Google Sheets, OCTO, Rezdy, scrapers)
  E. Cloud infrastructure (Railway API server, GitHub Pages, Cloudflare DNS)
  F. Social / distribution (Twitter, Reddit, Telegram)

Blocking vs non-blocking:
  BLOCKING   — Core pipeline + MCP + Supabase failures. A fix causing these is reverted.
  NON-BLOCKING — External APIs, cloud infra, social. Pre-existing issues; don't revert a good fix.

Exit codes:
  0 — All blocking checks passed (non-blocking failures logged but ignored)
  1 — One or more blocking checks failed
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.stdout.reconfigure(encoding="utf-8")

# ── Result tracking ───────────────────────────────────────────────────────────

_RESULTS: list[dict] = []   # {section, name, passed, blocking, detail}
_START_TIME = time.time()

BLOCKING     = True
NON_BLOCKING = False


def record(section: str, name: str, passed: bool, blocking: bool, detail: str = "") -> None:
    _RESULTS.append({
        "section":  section,
        "name":     name,
        "passed":   passed,
        "blocking": blocking,
        "detail":   detail,
    })
    icon   = "✓" if passed else ("✗" if blocking else "!")
    btype  = "BLOCK" if blocking else "soft "
    status = "PASS" if passed else "FAIL"
    print(f"  [{icon}] {name:<55} {status}  {detail[:80] if detail else ''}")


def skip(section: str, name: str, reason: str) -> None:
    _RESULTS.append({
        "section":  section,
        "name":     name,
        "passed":   None,
        "blocking": False,
        "detail":   f"SKIPPED: {reason}",
    })
    print(f"  [-] {name:<55} SKIP  {reason[:80]}")


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")

def _sb_headers() -> dict:
    secret = os.getenv("SUPABASE_SECRET_KEY", "")
    return {"apikey": secret, "Authorization": f"Bearer {secret}"}

def _sb_configured() -> bool:
    return bool(_sb_url() and os.getenv("SUPABASE_SECRET_KEY", ""))

# ── Test supplier names (must never appear in production) ─────────────────────

_TEST_SUPPLIER_NAMES = {
    "Zaui (Test)",
    "Edinburgh Explorer (Ventrata Test)",
    "Peek Pro (Test)",
}

# ── Section A: Core Python Pipeline ──────────────────────────────────────────

def check_aggregated_slots_file() -> None:
    """Aggregated slots file must exist and have required schema fields."""
    section = "A. Core Pipeline"
    path = Path(".tmp/aggregated_slots.json")
    if not path.exists():
        record(section, "Aggregated slots file exists", False, BLOCKING,
               ".tmp/aggregated_slots.json not found — run fetch + aggregate first")
        return
    record(section, "Aggregated slots file exists", True, BLOCKING)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        record(section, "Aggregated slots JSON parseable", False, BLOCKING, str(e)[:80])
        return
    record(section, "Aggregated slots JSON parseable", True, BLOCKING)

    if not isinstance(data, list) or len(data) == 0:
        record(section, "Aggregated slots non-empty list", False, BLOCKING,
               f"Got {type(data).__name__} with {len(data) if isinstance(data, list) else '?'} items")
        return
    record(section, "Aggregated slots non-empty list", True, BLOCKING, f"{len(data)} slots")

    # Schema check on sample
    required = {"slot_id", "platform", "category", "service_name", "start_time"}
    sample = data[:10]
    bad_slots = [s.get("slot_id", "?") for s in sample if not required.issubset(s.keys())]
    if bad_slots:
        missing = required - set(sample[0].keys())
        record(section, "Slot schema has required fields", False, BLOCKING,
               f"Missing: {missing} in {len(bad_slots)} sample slots")
    else:
        record(section, "Slot schema has required fields", True, BLOCKING,
               f"Checked {len(sample)} sample slots")

    # Pricing coverage
    priced = sum(1 for s in data if s.get("our_price") is not None)
    pct = priced / len(data) * 100 if data else 0
    ok = pct >= 95
    record(section, "Pricing coverage ≥95%", ok, BLOCKING,
           f"{priced}/{len(data)} = {pct:.1f}% priced")


def check_normalize_slot() -> None:
    """normalize_slot.py must process a raw dict without crashing."""
    section = "A. Core Pipeline"
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("normalize_slot",
                                            Path(__file__).parent / "normalize_slot.py")
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = {
            "service_name": "Test Tour",
            "category":     "experiences",
            "start_time":   datetime.now(timezone.utc).isoformat(),
            "price":        49.99,
            "business_name": "Test Supplier",
            "location_city": "Reykjavik",
            "location_country": "IS",
            "booking_url":  "https://example.com",
        }
        # normalize() requires platform as second positional arg
        result = mod.normalize(raw, "bokun")
        if not result.get("slot_id"):
            record(section, "normalize_slot produces slot_id", False, BLOCKING,
                   "slot_id empty after normalize()")
        else:
            record(section, "normalize_slot produces slot_id", True, BLOCKING,
                   result["slot_id"][:20])
    except Exception as e:
        record(section, "normalize_slot runs without error", False, BLOCKING, str(e)[:80])


def check_slot_behavioral_values() -> None:
    """Deeper behavioral correctness checks — prices sane, future slots exist, no duplicates, booking_url intact."""
    section = "A. Core Pipeline"
    path = Path(".tmp/aggregated_slots.json")
    if not path.exists():
        skip(section, "Slot behavioral value checks", ".tmp/aggregated_slots.json not found")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        skip(section, "Slot behavioral value checks", "JSON parse failed (checked above)")
        return
    if not isinstance(data, list) or not data:
        skip(section, "Slot behavioral value checks", "no slots to validate")
        return

    now_ts = datetime.now(timezone.utc).isoformat()

    # 1. Duplicate slot_ids
    ids = [s.get("slot_id") for s in data if s.get("slot_id")]
    dupes = len(ids) - len(set(ids))
    record(section, "No duplicate slot_ids in aggregated output",
           dupes == 0, BLOCKING,
           f"{dupes} duplicate(s) found" if dupes else f"{len(ids)} unique IDs")

    # 2. our_price <= price (no upcharging)
    priced = [s for s in data if s.get("our_price") is not None and s.get("price") is not None]
    upcharged = [s for s in priced
                 if float(s["our_price"]) > float(s["price"]) * 1.01]  # 1% tolerance for floats
    record(section, "our_price never exceeds face price",
           len(upcharged) == 0, BLOCKING,
           f"{len(upcharged)} slot(s) have our_price > price" if upcharged else f"OK across {len(priced)} priced slots")

    # 3. At least some slots in the future
    future = [s for s in data if s.get("start_time", "") > now_ts]
    pct_future = len(future) / len(data) * 100 if data else 0
    ok_future = pct_future >= 10   # at least 10% should be in the future
    record(section, "At least 10% of slots have future start_time",
           ok_future, BLOCKING,
           f"{pct_future:.1f}% future ({len(future)}/{len(data)})")

    # 4. booking_url integrity: bokun slots must have parseable booking_url JSON with required OCTO fields
    bokun_slots = [s for s in data if s.get("platform") in ("bokun", "octo")]
    bad_urls = []
    for s in bokun_slots[:50]:  # sample first 50
        url = s.get("booking_url", "")
        if not url:
            bad_urls.append(s.get("slot_id", "?"))
            continue
        # booking_url may be a JSON string with OCTO booking keys
        if url.startswith("{"):
            try:
                parsed = json.loads(url)
                if not parsed.get("product_id") and not parsed.get("option_id"):
                    bad_urls.append(s.get("slot_id", "?"))
            except Exception:
                bad_urls.append(s.get("slot_id", "?"))
    if bokun_slots:
        ok_urls = len(bad_urls) == 0
        record(section, "Bokun booking_url has OCTO fields (product_id/option_id)",
               ok_urls, BLOCKING,
               f"{len(bad_urls)} missing/malformed" if bad_urls else f"OK in {min(50, len(bokun_slots))} sampled")
    else:
        skip(section, "Bokun booking_url has OCTO fields", "no bokun/octo slots in output")


def check_booking_entry_points_static() -> None:
    """Static AST check: all 4 booking entry point functions must populate the canonical 20-field schema."""
    section = "A. Core Pipeline"
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "validate_pipeline", Path(__file__).parent / "validate_pipeline.py")
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        failures = mod.validate_booking_entry_points()
        if not failures:
            record(section, "All booking entry points populate 20-field canonical schema",
                   True, BLOCKING, "4 entry points validated")
        else:
            # Only blocking if HIGH or CRITICAL
            high = [f for f in failures if f.severity in ("HIGH", "CRITICAL")]
            msg = f"{len(failures)} gap(s) — {len(high)} HIGH/CRITICAL"
            record(section, "All booking entry points populate 20-field canonical schema",
                   len(high) == 0, BLOCKING, msg)
    except Exception as e:
        record(section, "Booking entry point schema check (static)",
               False, BLOCKING, str(e)[:80])


def check_circuit_breaker_read() -> None:
    """Circuit breaker module must load and read state without crashing."""
    section = "A. Core Pipeline"
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("circuit_breaker",
                                            Path(__file__).parent / "circuit_breaker.py")
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        blocked, reason = mod.is_open("test_health_check_supplier")
        record(section, "Circuit breaker read (no crash)", True, BLOCKING,
               f"state returned: blocked={blocked}")
    except Exception as e:
        record(section, "Circuit breaker read (no crash)", False, BLOCKING, str(e)[:80])


def check_wallet_schema() -> None:
    """manage_wallets.py must load wallet store without crashing."""
    section = "A. Core Pipeline"
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("manage_wallets",
                                            Path(__file__).parent / "manage_wallets.py")
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        wallets = mod._load_wallets()
        record(section, "Wallet store readable (no crash)", True, BLOCKING,
               f"{len(wallets)} wallet(s) on record")
    except Exception as e:
        record(section, "Wallet store readable (no crash)", False, BLOCKING, str(e)[:80])


# ── Section B: MCP Server (soft — skip if not running) ───────────────────────

def check_mcp_server() -> None:
    section = "B. MCP Server"
    mcp_port = int(os.getenv("MCP_HTTP_PORT", "5051"))
    # Quick TCP probe to see if something is listening
    reachable = False
    try:
        s = socket.create_connection(("localhost", mcp_port), timeout=3)
        s.close()
        reachable = True
    except Exception:
        pass

    if not reachable:
        skip(section, "MCP server reachable (localhost:{mcp_port})".replace("{mcp_port}", str(mcp_port)),
             "MCP server not running — start with: python tools/run_mcp_server.py --http")
        skip(section, "MCP search_slots tool", "MCP server not running")
        skip(section, "MCP get_slot tool", "MCP server not running")
        skip(section, "MCP get_booking_status tool", "MCP server not running")
        skip(section, "MCP book_slot tool registration", "MCP server not running")
        return

    base = f"http://localhost:{mcp_port}"
    try:
        r = requests.get(f"{base}/health", timeout=5)
        record(section, f"MCP server /health responds", r.status_code == 200, NON_BLOCKING,
               f"HTTP {r.status_code}")
    except Exception as e:
        record(section, "MCP server /health responds", False, NON_BLOCKING, str(e)[:60])
        return

    # search_slots
    try:
        r = requests.get(f"{base}/slots", params={"limit": 1}, timeout=8)
        ok = r.status_code == 200 and isinstance(r.json(), list)
        record(section, "MCP search_slots returns list", ok, NON_BLOCKING,
               f"HTTP {r.status_code}, type={type(r.json()).__name__}" if r.ok else r.text[:60])
    except Exception as e:
        record(section, "MCP search_slots returns list", False, NON_BLOCKING, str(e)[:60])

    # get_slot (use first slot_id from slots list if available)
    try:
        r = requests.get(f"{base}/slots", params={"limit": 1}, timeout=8)
        if r.status_code == 200 and r.json():
            sid = r.json()[0].get("slot_id", "")
            if sid:
                r2 = requests.get(f"{base}/slots/{sid}", timeout=8)
                ok = r2.status_code == 200 and r2.json().get("slot_id") == sid
                record(section, "MCP get_slot returns correct record", ok, NON_BLOCKING,
                       f"slot_id match={ok}")
            else:
                skip(section, "MCP get_slot returns correct record", "no slot_id in response")
        else:
            skip(section, "MCP get_slot returns correct record", "no slots in search results")
    except Exception as e:
        record(section, "MCP get_slot returns correct record", False, NON_BLOCKING, str(e)[:60])

    # get_booking_status (404 is acceptable — means endpoint exists but no such booking)
    try:
        r = requests.get(f"{base}/bookings/integration_test_probe", timeout=8)
        ok = r.status_code in (200, 404)
        record(section, "MCP get_booking_status endpoint exists", ok, NON_BLOCKING,
               f"HTTP {r.status_code} (404=expected for unknown id)")
    except Exception as e:
        record(section, "MCP get_booking_status endpoint exists", False, NON_BLOCKING, str(e)[:60])


# ── Section C: Supabase ───────────────────────────────────────────────────────

def check_supabase_slots() -> None:
    section = "C. Supabase"
    if not _sb_configured():
        skip(section, "Supabase slots table", "SUPABASE_URL or SUPABASE_SECRET_KEY not set")
        skip(section, "Slot schema valid in Supabase", "credentials not set")
        skip(section, "No test slots in production", "credentials not set")
        return

    try:
        r = requests.get(
            f"{_sb_url()}/rest/v1/slots",
            headers={**_sb_headers(), "Range": "0-9", "Prefer": "count=exact"},
            params={"select": "slot_id,platform,category,service_name,start_time,our_price",
                    "limit": "10"},
            timeout=10,
        )
        if r.status_code not in (200, 206):
            record(section, "Supabase slots table readable", False, BLOCKING,
                   f"HTTP {r.status_code}: {r.text[:80]}")
            return
        rows = r.json()
        total_hdr = r.headers.get("Content-Range", "?")
        record(section, "Supabase slots table readable", True, BLOCKING,
               f"Content-Range: {total_hdr}, sample={len(rows)} rows")
    except Exception as e:
        record(section, "Supabase slots table readable", False, BLOCKING, str(e)[:80])
        return

    # Schema check
    required = {"slot_id", "platform", "category", "service_name", "start_time"}
    if rows:
        bad = [row.get("slot_id", "?") for row in rows if not required.issubset(row.keys())]
        record(section, "Supabase slot schema valid", len(bad) == 0, BLOCKING,
               f"All {len(rows)} sampled rows have required fields" if not bad else f"{len(bad)} rows missing fields")
    else:
        skip(section, "Supabase slot schema valid", "no rows returned to sample")

    # Test supplier leak check
    try:
        test_names_filter = ",".join(f'"{n}"' for n in _TEST_SUPPLIER_NAMES)
        r2 = requests.get(
            f"{_sb_url()}/rest/v1/slots",
            headers={**_sb_headers(), "Prefer": "count=exact"},
            params={"select": "slot_id,business_name",
                    "business_name": f"in.({test_names_filter})",
                    "limit": "1"},
            timeout=10,
        )
        if r2.status_code in (200, 206):
            test_rows = r2.json()
            ok = len(test_rows) == 0
            record(section, "No test supplier slots in production", ok, BLOCKING,
                   "clean" if ok else f"{len(test_rows)} test slot(s) found! Run sync_to_supabase cleanup.")
        else:
            record(section, "No test supplier slots in production", False, NON_BLOCKING,
                   f"Could not query: HTTP {r2.status_code}")
    except Exception as e:
        record(section, "No test supplier slots in production", False, NON_BLOCKING, str(e)[:60])


def check_supabase_bookings() -> None:
    # Bookings are stored in Supabase Storage (bookings/{id}.json), not a REST table.
    section = "C. Supabase"
    if not _sb_configured():
        skip(section, "Supabase bookings storage accessible", "credentials not set")
        return
    try:
        r = requests.post(
            f"{_sb_url()}/storage/v1/object/list/bookings",
            headers={**_sb_headers(), "Content-Type": "application/json"},
            json={"prefix": "bookings/", "limit": 1},
            timeout=8,
        )
        # 200 = bucket accessible (even if 0 bookings), 400 = bucket config issue
        ok = r.status_code == 200
        try:
            count = len(r.json()) if ok else "?"
        except Exception:
            count = "?"
        record(section, "Supabase bookings storage accessible", ok, BLOCKING,
               f"{count} booking records" if ok else f"HTTP {r.status_code}: {r.text[:60]}")
    except Exception as e:
        record(section, "Supabase bookings storage accessible", False, BLOCKING, str(e)[:60])


def check_supabase_wallets() -> None:
    section = "C. Supabase"
    if not _sb_configured():
        skip(section, "Supabase wallets (Storage) readable", "credentials not set")
        return
    # Wallets stored in Supabase Storage at config/wallets.json
    try:
        r = requests.get(
            f"{_sb_url()}/storage/v1/object/bookings/config/wallets.json",
            headers=_sb_headers(),
            timeout=8,
        )
        # 200 = file exists, 404 = no wallets yet (acceptable)
        ok = r.status_code in (200, 404)
        detail = "wallets.json found" if r.status_code == 200 else "no wallets yet (404 — OK)"
        record(section, "Supabase wallets storage accessible", ok, BLOCKING, detail)
    except Exception as e:
        record(section, "Supabase wallets storage accessible", False, BLOCKING, str(e)[:60])


def check_supabase_storage_buckets() -> None:
    section = "C. Supabase"
    if not _sb_configured():
        skip(section, "Supabase Storage circuit_breaker/ accessible", "credentials not set")
        skip(section, "Supabase Storage cancellation_queue/ accessible", "credentials not set")
        return
    for prefix, name in [("circuit_breaker/", "circuit_breaker/"), ("cancellation_queue/", "cancellation_queue/")]:
        try:
            r = requests.post(
                f"{_sb_url()}/storage/v1/object/list/bookings",
                headers={**_sb_headers(), "Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 1},
                timeout=8,
            )
            ok = r.status_code == 200
            detail = f"{len(r.json())} items" if ok else f"HTTP {r.status_code}: {r.text[:40]}"
            record(section, f"Supabase Storage {name} accessible", ok, BLOCKING, detail)
        except Exception as e:
            record(section, f"Supabase Storage {name} accessible", False, BLOCKING, str(e)[:60])


# ── Section D: External APIs ──────────────────────────────────────────────────

def check_stripe() -> None:
    section = "D. External APIs"
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        skip(section, "Stripe API key valid", "STRIPE_SECRET_KEY not set")
        return
    try:
        import stripe as _stripe
        _stripe.api_key = key
        bal = _stripe.Balance.retrieve()
        record(section, "Stripe API key valid", True, NON_BLOCKING,
               f"available: {bal['available'][0]['amount']/100:.2f} {bal['available'][0]['currency'].upper()}")
    except ImportError:
        skip(section, "Stripe API key valid", "stripe library not installed")
    except Exception as e:
        record(section, "Stripe API key valid", False, NON_BLOCKING, str(e)[:80])


def check_sendgrid() -> None:
    section = "D. External APIs"
    key = os.getenv("SENDGRID_API_KEY", "").strip()
    if not key:
        skip(section, "SendGrid API key valid", "SENDGRID_API_KEY not set")
        return
    try:
        r = requests.get(
            "https://api.sendgrid.com/v3/user/account",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        ok = r.status_code == 200
        detail = r.json().get("account_type", "unknown") if ok else f"HTTP {r.status_code}"
        record(section, "SendGrid API key valid", ok, NON_BLOCKING, detail)
    except Exception as e:
        record(section, "SendGrid API key valid", False, NON_BLOCKING, str(e)[:60])


def check_twilio() -> None:
    section = "D. External APIs"
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        skip(section, "Twilio credentials valid", "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
        return
    try:
        r = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(sid, token),
            timeout=10,
        )
        ok = r.status_code == 200
        detail = r.json().get("status", "?") if ok else f"HTTP {r.status_code}"
        record(section, "Twilio credentials valid", ok, NON_BLOCKING, detail)
    except Exception as e:
        record(section, "Twilio credentials valid", False, NON_BLOCKING, str(e)[:60])


def check_google_sheets_oauth() -> None:
    section = "D. External APIs"
    token_path = Path("token.json")
    if not token_path.exists():
        skip(section, "Google Sheets OAuth token valid", "token.json not found — run setup_google_sheets.py")
        return
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            record(section, "Google Sheets OAuth token valid", True, NON_BLOCKING,
                   "Token was expired — refreshed successfully")
        elif creds.valid:
            record(section, "Google Sheets OAuth token valid", True, NON_BLOCKING, "Token valid")
        else:
            record(section, "Google Sheets OAuth token valid", False, NON_BLOCKING,
                   "Token expired, no refresh_token — re-auth required")
    except ImportError:
        skip(section, "Google Sheets OAuth token valid", "google-auth library not installed")
    except Exception as e:
        record(section, "Google Sheets OAuth token valid", False, NON_BLOCKING, str(e)[:80])


def check_bokun_octo() -> None:
    section = "D. External APIs"
    api_key = os.getenv("BOKUN_API_KEY", "").strip()
    if not api_key:
        skip(section, "Bokun OCTO API reachable", "BOKUN_API_KEY not set")
        return
    try:
        # Use /products endpoint (no Octo-Capabilities header — it causes Bokun to hang)
        r = requests.get(
            "https://api.bokun.io/octo/v1/products",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=20,
        )
        ok = r.status_code == 200
        if ok:
            try:
                count = len(r.json())
                detail = f"{count} products"
            except Exception:
                detail = "reachable"
        else:
            detail = f"HTTP {r.status_code}: {r.text[:60]}"
        record(section, "Bokun OCTO API reachable", ok, NON_BLOCKING, detail)
    except Exception as e:
        record(section, "Bokun OCTO API reachable", False, NON_BLOCKING, str(e)[:60])


def check_rezdy() -> None:
    section = "D. External APIs"
    api_key = os.getenv("REZDY_API_KEY", "").strip()
    if not api_key:
        skip(section, "Rezdy API reachable", "REZDY_API_KEY not set")
        return
    try:
        r = requests.get(
            "https://api.rezdy.com/v1/products",
            params={"apiKey": api_key, "limit": 1},
            timeout=12,
        )
        ok = r.status_code == 200
        detail = "reachable" if ok else f"HTTP {r.status_code}: {r.text[:60]}"
        record(section, "Rezdy API reachable", ok, NON_BLOCKING, detail)
    except Exception as e:
        record(section, "Rezdy API reachable", False, NON_BLOCKING, str(e)[:60])


def check_eventbrite_scrapeable() -> None:
    section = "D. External APIs"
    try:
        r = requests.get(
            "https://www.eventbrite.com/d/ny--new-york/all-events/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0"},
            timeout=15,
        )
        has_data = r.status_code == 200 and "__SERVER_DATA__" in r.text
        if r.status_code != 200:
            record(section, "Eventbrite scrape page accessible", False, NON_BLOCKING, f"HTTP {r.status_code}")
        elif not has_data:
            record(section, "Eventbrite scrape page accessible", False, NON_BLOCKING,
                   "__SERVER_DATA__ missing — page structure may have changed")
        else:
            record(section, "Eventbrite scrape page accessible", True, NON_BLOCKING,
                   "__SERVER_DATA__ present")
    except Exception as e:
        record(section, "Eventbrite scrape page accessible", False, NON_BLOCKING, str(e)[:60])


def check_meetup_scrapeable() -> None:
    section = "D. External APIs"
    try:
        r = requests.get(
            "https://www.meetup.com/find/us--ny--new-york/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0"},
            timeout=15,
        )
        has_data = r.status_code == 200 and "__NEXT_DATA__" in r.text
        if r.status_code != 200:
            record(section, "Meetup scrape page accessible", False, NON_BLOCKING, f"HTTP {r.status_code}")
        elif not has_data:
            record(section, "Meetup scrape page accessible", False, NON_BLOCKING,
                   "__NEXT_DATA__ missing — page structure may have changed")
        else:
            record(section, "Meetup scrape page accessible", True, NON_BLOCKING,
                   "__NEXT_DATA__ present")
    except Exception as e:
        record(section, "Meetup scrape page accessible", False, NON_BLOCKING, str(e)[:60])


# ── Section E: Cloud Infrastructure ──────────────────────────────────────────

def check_railway_server() -> None:
    section = "E. Cloud Infra"
    host = os.getenv("BOOKING_SERVER_HOST", "").strip().rstrip("/")
    if not host:
        skip(section, "Railway API server /health", "BOOKING_SERVER_HOST not set")
        return
    try:
        r = requests.get(f"{host}/health", timeout=12)
        ok = r.status_code == 200
        record(section, "Railway API server /health", ok, NON_BLOCKING,
               f"HTTP {r.status_code}")
    except Exception as e:
        record(section, "Railway API server /health", False, NON_BLOCKING, str(e)[:60])


def check_landing_page() -> None:
    section = "E. Cloud Infra"
    landing = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").strip()
    try:
        r = requests.get(landing, timeout=15)
        ok = r.status_code == 200
        record(section, f"Landing page accessible ({landing})", ok, NON_BLOCKING,
               f"HTTP {r.status_code}, {len(r.content)//1024}KB")
    except Exception as e:
        record(section, f"Landing page accessible", False, NON_BLOCKING, str(e)[:60])


def check_cloudflare_dns() -> None:
    section = "E. Cloud Infra"
    domain = "lastminutedealshq.com"
    try:
        ip = socket.gethostbyname(domain)
        record(section, f"Cloudflare DNS resolves ({domain})", True, NON_BLOCKING,
               f"resolves to {ip}")
    except Exception as e:
        record(section, f"Cloudflare DNS resolves ({domain})", False, NON_BLOCKING, str(e)[:60])


# ── Section F: Social / Distribution ─────────────────────────────────────────

def check_telegram() -> None:
    section = "F. Social"
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        skip(section, "Telegram bot credentials valid", "TELEGRAM_BOT_TOKEN not set")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        ok = data.get("ok", False)
        detail = f"@{data['result']['username']}" if ok else data.get("description", r.text[:40])
        record(section, "Telegram bot credentials valid", ok, NON_BLOCKING, detail)
    except Exception as e:
        record(section, "Telegram bot credentials valid", False, NON_BLOCKING, str(e)[:60])


def check_twitter() -> None:
    section = "F. Social"
    key    = os.getenv("TWITTER_API_KEY", "").strip()
    secret = os.getenv("TWITTER_API_SECRET", "").strip()
    if not key or not secret:
        skip(section, "Twitter/X app credentials valid", "TWITTER_API_KEY or TWITTER_API_SECRET not set")
        return
    try:
        r = requests.post(
            "https://api.twitter.com/oauth2/token",
            auth=(key, secret),
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("token_type") == "bearer"
        record(section, "Twitter/X app credentials valid", ok, NON_BLOCKING,
               "bearer token obtained" if ok else f"HTTP {r.status_code}: {r.text[:40]}")
    except Exception as e:
        record(section, "Twitter/X app credentials valid", False, NON_BLOCKING, str(e)[:60])


def check_reddit() -> None:
    section = "F. Social"
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    secret    = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    username  = os.getenv("REDDIT_USERNAME", "").strip()
    password  = os.getenv("REDDIT_PASSWORD", "").strip()
    if not all([client_id, secret, username, password]):
        skip(section, "Reddit credentials valid", "REDDIT_* env vars not fully set")
        return
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, secret),
            data={"grant_type": "password", "username": username, "password": password},
            headers={"User-Agent": "LastMinuteDealsHQ/1.0"},
            timeout=10,
        )
        ok = r.status_code == 200 and "access_token" in r.json()
        record(section, "Reddit credentials valid", ok, NON_BLOCKING,
               "auth OK" if ok else f"HTTP {r.status_code}: {r.json().get('error', r.text[:40])}")
    except Exception as e:
        record(section, "Reddit credentials valid", False, NON_BLOCKING, str(e)[:60])


# ── Flask API server (local) ──────────────────────────────────────────────────

def check_local_api_server() -> None:
    """Check if local Flask API server is running (soft check — it's a long-running process)."""
    section = "E. Cloud Infra"
    local_port = int(os.getenv("API_SERVER_PORT", "5050"))
    try:
        s = socket.create_connection(("localhost", local_port), timeout=2)
        s.close()
        r = requests.get(f"http://localhost:{local_port}/health", timeout=5)
        ok = r.status_code == 200
        record(section, f"Local API server /health (:{local_port})", ok, NON_BLOCKING,
               f"HTTP {r.status_code}")
    except Exception:
        skip(section, f"Local API server /health (:{local_port})",
             "not running (start with: python tools/run_api_server.py)")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_all(blocking_only: bool = False) -> dict:
    """Run all integration checks. Returns summary dict."""
    print(f"\n{'='*70}")
    print(f"  Integration Test — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")

    print("\nA. Core Python Pipeline")
    check_aggregated_slots_file()
    check_slot_behavioral_values()
    check_booking_entry_points_static()
    check_normalize_slot()
    check_circuit_breaker_read()
    check_wallet_schema()

    if not blocking_only:
        print("\nB. MCP Server")
        check_mcp_server()

        print("\nC. Supabase")
    else:
        print("\nC. Supabase")
    check_supabase_slots()
    check_supabase_bookings()
    check_supabase_wallets()
    check_supabase_storage_buckets()

    if not blocking_only:
        print("\nD. External APIs")
        check_stripe()
        check_sendgrid()
        check_twilio()
        check_google_sheets_oauth()
        check_bokun_octo()
        check_rezdy()
        check_eventbrite_scrapeable()
        check_meetup_scrapeable()

        print("\nE. Cloud Infrastructure")
        check_railway_server()
        check_local_api_server()
        check_landing_page()
        check_cloudflare_dns()

        print("\nF. Social / Distribution")
        check_telegram()
        check_twitter()
        check_reddit()

    # Summary
    elapsed = time.time() - _START_TIME
    blocking_failures = [r for r in _RESULTS if r["blocking"] and r["passed"] is False]
    soft_failures     = [r for r in _RESULTS if not r["blocking"] and r["passed"] is False]
    passed            = [r for r in _RESULTS if r["passed"] is True]
    skipped           = [r for r in _RESULTS if r["passed"] is None]

    print(f"\n{'='*70}")
    print(f"  Results ({elapsed:.1f}s)")
    print(f"  Passed:             {len(passed)}")
    print(f"  Blocking failures:  {len(blocking_failures)}")
    print(f"  Soft failures:      {len(soft_failures)}")
    print(f"  Skipped:            {len(skipped)}")

    if blocking_failures:
        print(f"\n  BLOCKING FAILURES (will revert any fix that introduced these):")
        for r in blocking_failures:
            print(f"    ✗ [{r['section']}] {r['name']}: {r['detail']}")

    if soft_failures:
        print(f"\n  Soft failures (pre-existing, not caused by fixes):")
        for r in soft_failures:
            print(f"    ! [{r['section']}] {r['name']}: {r['detail']}")

    overall = "PASS" if not blocking_failures else "FAIL"
    print(f"\n  Overall: {overall} {'✓' if overall == 'PASS' else '✗'}")
    print(f"{'='*70}\n")

    return {
        "overall":            overall,
        "passed":             len(passed),
        "blocking_failures":  len(blocking_failures),
        "soft_failures":      len(soft_failures),
        "skipped":            len(skipped),
        "blocking_fail_list": [{"name": r["name"], "detail": r["detail"]} for r in blocking_failures],
        "soft_fail_list":     [{"name": r["name"], "detail": r["detail"]} for r in soft_failures],
        "elapsed_seconds":    round(elapsed, 1),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Full-surface integration test for LastMinuteDeals")
    parser.add_argument("--blocking-only", action="store_true",
                        help="Run only blocking checks (faster — used by fix loop after each attempt)")
    parser.add_argument("--json", action="store_true",
                        help="Output final summary as JSON to stdout")
    args = parser.parse_args()

    summary = run_all(blocking_only=args.blocking_only)

    if args.json:
        print(json.dumps(summary, indent=2))

    sys.exit(0 if summary["overall"] == "PASS" else 1)


if __name__ == "__main__":
    main()
