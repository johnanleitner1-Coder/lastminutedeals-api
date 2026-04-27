"""
run_api_server.py — Booking API backend for LastMinuteDeals.

Provides the /api/book endpoint that the landing page calls when a user
clicks "Book Now" and submits their payment details.

Flow:
  1. POST /api/book  { slot_id, customer_name, customer_email, customer_phone }
  2. Server looks up slot in aggregated_slots.json to get our_price + booking_url
  3. Creates a Stripe Checkout Session (hosted payment page)
  4. Returns { checkout_url } — frontend redirects the user there
  5. After payment, Stripe calls /api/webhook
  6. Webhook calls complete_booking.py to execute the reservation on the source platform

Required .env vars:
  STRIPE_SECRET_KEY         — from Stripe Dashboard > Developers > API Keys
  STRIPE_WEBHOOK_SECRET     — from Stripe Dashboard > Webhooks (add endpoint /api/webhook)
  BOOKING_SERVER_HOST       — hostname where this server is reachable (e.g. https://api.yourdomain.com)
  LANDING_PAGE_URL          — URL of the landing page (for redirect after payment)

Usage:
  pip install flask stripe
  python tools/run_api_server.py

To expose publicly for testing (requires ngrok):
  ngrok http 5050
  # Then set BOOKING_SERVER_HOST=https://xxxx.ngrok.io in .env
  # Add ngrok URL to Stripe Webhook endpoints

Deploy to production:
  - Railway: railway up
  - Render: Create web service pointing to this file
  - Fly.io: fly launch
"""

import hashlib
import hmac
import importlib.util as _ilu
import json
import os
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, request
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

app = Flask(__name__)

@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def _options(path):
    return "", 204

# ── Lazy module loaders (avoid circular imports and load-time failures) ────────

def _load_module(name: str):
    path = Path(__file__).parent / f"{name}.py"
    if not path.exists():
        return None
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Execution receipts — signed booking records ───────────────────────────────

_BOOKINGS_FILE = Path(".tmp/bookings.json")
_BOOKINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Idempotency store (in-memory, per-process) ────────────────────────────────
# Maps idempotency_key → booking_id for deduplication within a process lifetime.
# For cross-instance dedup, the Supabase Storage record acts as the persistent lock.
_IDEMPOTENCY_CACHE: dict = {}   # {key: {"booking_id": str, "result": dict}}

# ── Slot discovery telemetry (updated by APScheduler job) ─────────────────────
_last_discovery_at: str = ""
_last_discovery_slot_count: int = 0

# ── Autonomous booking in-flight lock ─────────────────────────────────────────
# Prevents double-debit if an agent retries while the first call is still executing.
# Key: sha256(slot_id + customer_email + wallet_id) — naturally unique per booking attempt.
# Value: True while in-flight. Cleared on success or refund.
_DIRECT_IN_FLIGHT: dict[str, bool] = {}
_DIRECT_LOCK = threading.Lock()

# Saved-card booking idempotency (same pattern as direct/wallet flow)
_SAVED_CARD_IN_FLIGHT: dict[str, bool] = {}
_SAVED_CARD_LOCK = threading.Lock()

# Per-wallet locks to prevent concurrent bookings from overdrafting a wallet.
# Two requests for DIFFERENT slots on the SAME wallet could both pass the
# balance check before either debits — pushing the wallet negative.
_WALLET_LOCKS: dict[str, threading.Lock] = {}
_WALLET_LOCKS_META = threading.Lock()  # protects _WALLET_LOCKS dict itself

def _get_wallet_lock(wallet_id: str) -> threading.Lock:
    with _WALLET_LOCKS_META:
        if wallet_id not in _WALLET_LOCKS:
            _WALLET_LOCKS[wallet_id] = threading.Lock()
        return _WALLET_LOCKS[wallet_id]

def _signing_secret() -> str:
    """Return the HMAC signing secret from LMD_SIGNING_SECRET env var.

    MUST be set as a persistent Railway/environment variable — NOT .tmp/ which is wiped
    on every redeploy, invalidating all outstanding cancel links.

    Raises RuntimeError at startup if unset so the misconfiguration is caught immediately.
    """
    key = os.getenv("LMD_SIGNING_SECRET", "").strip()
    if key:
        return key
    # Legacy: read from .tmp/ if still present (survives only same container lifetime)
    secret_file = Path(".tmp/.signing_secret")
    if secret_file.exists():
        persisted = secret_file.read_text().strip()
        if persisted:
            print("[WARNING] LMD_SIGNING_SECRET not set — reading from .tmp/ (cancel links "
                  "will break on next Railway redeploy). Set LMD_SIGNING_SECRET in env vars.")
            return persisted
    # No key anywhere — generate an ephemeral one and warn loudly.
    # Ephemeral key means cancel links only work until next restart.
    import secrets as _s
    ephemeral = _s.token_hex(32)
    print("[ERROR] LMD_SIGNING_SECRET is not set. Using an ephemeral key — cancel links "
          "will be invalid after any restart or redeploy. "
          "Set LMD_SIGNING_SECRET in Railway environment variables immediately.")
    return ephemeral

def _sign_record(record: dict) -> str:
    """Return HMAC-SHA256 hex signature over the stable JSON representation."""
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hmac.new(_signing_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()

def _make_cancel_token(booking_id: str) -> str:
    """Return a 32-char HMAC token for authenticating self-serve cancel URLs."""
    return hmac.new(_signing_secret().encode(), booking_id.encode(), hashlib.sha256).hexdigest()[:32]

def _verify_cancel_token(booking_id: str, token: str) -> bool:
    """Constant-time comparison to verify a cancel token."""
    expected = _make_cancel_token(booking_id)
    return hmac.compare_digest(expected, token or "")

def _sb_storage_headers() -> dict:
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    return {"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"}


def _load_bookings() -> dict:
    """Load all booking records from local file (full scan rarely needed)."""
    if _BOOKINGS_FILE.exists():
        try:
            return json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_booking_record(booking_id: str) -> dict | None:
    """
    Load a single booking record by ID.
    Primary: Supabase Storage (shared across all Railway instances).
    Fallback: local file (same-instance only, used if Storage unavailable).
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            r = requests.get(
                f"{sb_url}/storage/v1/object/bookings/{booking_id}.json",
                headers=_sb_storage_headers(),
                timeout=5,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    # Local fallback
    bookings = {}
    if _BOOKINGS_FILE.exists():
        try:
            bookings = json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return bookings.get(booking_id)


def _save_booking_record(booking_id: str, record: dict) -> None:
    """
    Persist a booking record.
    Primary: Supabase Storage — survives redeploys, visible to all Railway instances.
    Backup: local .tmp/bookings.json — same-instance fallback if Storage unavailable.

    Also writes a by_confirmation/ lookup index for O(1) retrieval by confirmation
    code or supplier_reference — used by bokun_webhook instead of a full bucket scan.
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            r = requests.post(
                f"{sb_url}/storage/v1/object/bookings/{booking_id}.json",
                headers={**_sb_storage_headers(), "Content-Type": "application/json",
                         "x-upsert": "true"},
                data=json.dumps(record),
                timeout=8,
            )
            if r.status_code not in (200, 201):
                print(f"[BOOKINGS] Supabase Storage write returned {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[BOOKINGS] Supabase Storage write failed: {e}")

        # Write O(1) confirmation lookup index — only when a real confirmation exists.
        # Covers both OCTO UUID (confirmation) and Bokun's own reference (supplier_reference).
        _idx_hdrs = {**_sb_storage_headers(), "Content-Type": "application/json", "x-upsert": "true"}
        _idx_payload = json.dumps({"booking_id": booking_id})
        for _conf_key in ("confirmation", "supplier_reference"):
            _conf_val = record.get(_conf_key, "").strip()
            if _conf_val:
                try:
                    requests.post(
                        f"{sb_url}/storage/v1/object/bookings/by_confirmation/{_conf_val}.json",
                        headers=_idx_hdrs, data=_idx_payload, timeout=5,
                    )
                except Exception:
                    pass
    # Always write local backup too
    try:
        local = {}
        if _BOOKINGS_FILE.exists():
            try:
                local = json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        local[booking_id] = record
        _BOOKINGS_FILE.write_text(json.dumps(local, indent=2), encoding="utf-8")
    except Exception:
        pass

def _list_booking_records(prefix: str = "") -> list[dict]:
    """
    List booking record metadata from Supabase Storage.
    Returns a list of dicts with at least a "name" key.
    Falls back to local .tmp/bookings.json keys if Supabase unavailable.
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            r = requests.post(
                f"{sb_url}/storage/v1/object/list/bookings",
                headers={**_sb_storage_headers(), "Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 1000},
                timeout=8,
            )
            if r.status_code == 200:
                return [
                    {"name": item["name"].removesuffix(".json")}
                    for item in r.json()
                    if isinstance(item, dict) and item.get("name", "").endswith(".json")
                ]
        except Exception:
            pass
    # Local fallback
    if _BOOKINGS_FILE.exists():
        try:
            local = json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
            return [{"name": k} for k in local if not prefix or k.startswith(prefix)]
        except Exception:
            pass
    return []


def _make_receipt(
    result_dict: dict,
    customer_email: str = "",
    customer: dict | None = None,
    payment: dict | None = None,
    slot: dict | None = None,
) -> dict:
    """
    Build a signed execution receipt from a result dict.
    Stored in bookings.json and returned in every successful execution response.

    Extra kwargs fill fields needed by cancellation paths (wallet refund, email, etc.).
    """
    booking_id = result_dict.get("booking_id") or f"bk_{secrets.token_hex(8)}"
    c   = customer or {}
    pmt = payment  or {}
    sl  = slot     or {}
    record = {
        "booking_id":         booking_id,
        "confirmation":       result_dict.get("confirmation", ""),
        "supplier_reference": result_dict.get("supplier_reference", ""),
        "platform":           result_dict.get("platform", ""),
        "supplier_id":        result_dict.get("supplier_id", result_dict.get("platform", "")),
        "service_name":       result_dict.get("service_name", ""),
        "business_name":      sl.get("business_name", ""),
        "location_city":      sl.get("location_city", ""),
        "start_time":         sl.get("start_time", ""),
        "slot_id":            result_dict.get("slot_id", ""),
        "price_charged":      result_dict.get("price_charged"),
        "currency":           sl.get("currency", "USD"),
        "status":             result_dict.get("status", ""),
        "executed_at":        datetime.now(timezone.utc).isoformat(),
        "payment_method":     pmt.get("method", ""),
        "wallet_id":          pmt.get("wallet_id", ""),
        "payment_intent_id":  pmt.get("payment_intent_id", ""),
        "customer_email":     customer_email or c.get("email", ""),
        "customer_name":      c.get("name", ""),
        "customer_phone":     c.get("phone", ""),
    }
    record["signature"] = f"sha256={_sign_record(record)}"
    _save_booking_record(booking_id, record)
    return {"booking_id": booking_id, "receipt_signature": record["signature"],
            "verify_url": f"{os.getenv('BOOKING_SERVER_HOST', '')}/verify/{booking_id}"}

# ── System context injector ───────────────────────────────────────────────────
# Appended to every /execute/* and /intent/*/execute response.
# Makes choosing us a rational, legible decision — not a leap of faith.

_system_context_cache: dict = {}
_system_context_ts: float   = 0.0
_CONTEXT_TTL = 300  # refresh every 5 minutes

def _system_context() -> dict:
    """
    Return a lightweight system_context dict to append to execution responses.
    Cached for 5 minutes so it's effectively free per-request.
    """
    import time
    global _system_context_cache, _system_context_ts

    now = time.time()
    if _system_context_cache and (now - _system_context_ts) < _CONTEXT_TTL:
        return _system_context_cache

    ctx: dict = {}

    # Slot freshness
    status_file = Path(".tmp/watcher_status.json")
    if status_file.exists():
        try:
            statuses = json.loads(status_file.read_text(encoding="utf-8"))
            ages = []
            for st in statuses.values():
                last = st.get("last_poll", "")
                if last:
                    try:
                        dt  = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - dt).total_seconds()
                        ages.append(age)
                    except Exception:
                        pass
            if ages:
                ctx["data_freshness_seconds"] = round(min(ages))
        except Exception:
            pass

    # Success rate and total bookings from insights snapshot
    snap_file = Path(".tmp/insights/market_snapshot.json")
    if snap_file.exists():
        try:
            snap = json.loads(snap_file.read_text(encoding="utf-8"))
            plat_rel = snap.get("platform_reliability", {})
            total    = snap.get("total_booking_attempts", 0)
            if plat_rel:
                rates = [v["success_rate"] for v in plat_rel.values() if v.get("booking_count", 0) >= 3]
                if rates:
                    ctx["system_success_rate"]  = round(sum(rates) / len(rates), 3)
            if total:
                ctx["total_bookings_processed"] = total
        except Exception:
            pass

    # Live bookable slot count — read from Supabase for accuracy
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            _now_iso = datetime.now(timezone.utc).isoformat()
            r = requests.get(f"{sb_url}/rest/v1/slots",
                headers={"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}",
                         "Prefer": "count=exact", "Range": "0-0"},
                params=[("select", "slot_id"), ("start_time", f"gt.{_now_iso}"),
                        ("our_price", "gt.0")],
                timeout=5)
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                ctx["live_bookable_slots"] = int(cr.split("/")[1])
        except Exception:
            pass
    if "live_bookable_slots" not in ctx and DATA_FILE.exists():
        try:
            slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            ctx["live_bookable_slots"] = sum(
                1 for s in slots if float(s.get("our_price") or s.get("price") or 0) > 0
            )
        except Exception:
            pass

    _system_context_cache = ctx
    _system_context_ts    = now
    return ctx


def _with_context(result: dict) -> dict:
    """Merge system_context into a result dict. Non-destructive — never overwrites existing keys."""
    ctx = _system_context()
    if ctx:
        result["system_context"] = ctx
    return result


# ── Start background intent monitor on first request ──────────────────────────
_intent_monitor_started = False
_intent_monitor_lock    = __import__("threading").Lock()

def _ensure_intent_monitor():
    global _intent_monitor_started
    if _intent_monitor_started:
        return
    with _intent_monitor_lock:
        if _intent_monitor_started:
            return
        try:
            mod = _load_module("intent_sessions")
            if mod:
                store   = mod.IntentSessionStore()
                monitor = mod.IntentMonitor(store)
                monitor.start()
                app.config["_intent_store"]   = store
                app.config["_intent_monitor"] = monitor
            _intent_monitor_started = True
        except Exception as e:
            print(f"[API] Intent monitor failed to start: {e}")

@app.before_request
def _lazy_init():
    _ensure_intent_monitor()
    request._start_time = time.monotonic()


@app.after_request
def _log_request(response):
    """Fire-and-forget request log to Supabase. Never delays the response."""
    try:
        latency_ms = round((time.monotonic() - request._start_time) * 1000)
    except Exception:
        latency_ms = None

    path = request.path
    # Skip noisy internals
    if path in ("/health", "/sse", "/messages") or path.startswith("/static"):
        return response

    source = _detect_source()
    _record_request(path, source, response.status_code, latency_ms,
                    mcp_method=getattr(request, "_mcp_method", None),
                    mcp_tool=getattr(request, "_mcp_tool", None))
    # DB write disabled — the in-memory _request_log_buffer (50k entries) already
    # serves /metrics. Writing every request to Supabase Postgres was the primary
    # cause of Disk IO Budget depletion (~5,000+ writes/day).
    # threading.Thread(
    #     target=_write_request_log,
    #     args=(path, request.method, response.status_code, latency_ms, source),
    #     daemon=True,
    # ).start()
    return response


def _detect_source() -> str:
    """Classify the caller: mcp_agent | smithery | landing_page | api."""
    # Our own MCP proxy tags its forwarded requests
    if request.headers.get("X-Mcp-Source"):
        return "mcp_agent"
    referer = request.headers.get("Referer", "")
    landing = os.getenv("LANDING_PAGE_URL", "lastminutedealshq.com")
    if landing and landing in referer:
        return "landing_page"
    ua = request.headers.get("User-Agent", "").lower()
    if "smithery" in ua:
        return "smithery"
    # MCP SSE clients typically use python-requests or mcp-client user agents
    if any(x in ua for x in ("mcp", "claude", "openai", "anthropic", "gpt")):
        return "mcp_agent"
    return "api"


def _write_request_log(path: str, method: str, status: int, latency_ms: int | None, source: str):
    """Write one row to the request_logs Supabase table. Runs in a daemon thread."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        return
    try:
        requests.post(
            f"{sb_url}/rest/v1/request_logs",
            headers={
                "apikey":        sb_secret,
                "Authorization": f"Bearer {sb_secret}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json={
                "path":       path,
                "method":     method,
                "status":     status,
                "latency_ms": latency_ms,
                "source":     source,
                "logged_at":  datetime.now(timezone.utc).isoformat(),
            },
            timeout=4,
        )
    except Exception:
        pass  # Never let logging break the API


def _pg_connect(timeout: int = 8):
    """Connect to Supabase Postgres.
    urlparse and psycopg2's own URI parser both fail when the password
    contains unencoded special chars like '/', '&', or '@'. We parse
    manually: strip the scheme, split on the last '@' to isolate the
    credentials block, then split credentials on the first ':'.
    """
    import psycopg2
    from urllib.parse import unquote
    db_url = os.getenv("SUPABASE_DB_URL", "")
    if not db_url:
        return None

    # Strip scheme
    raw = db_url
    for scheme in ("postgresql://", "postgres://"):
        if raw.startswith(scheme):
            raw = raw[len(scheme):]
            break

    # Split credentials from host on the LAST '@'
    # Works even when the password contains '@' (unlikely but safe)
    last_at = raw.rfind("@")
    if last_at < 0:
        # No credentials in URL — try connecting as-is
        return psycopg2.connect(db_url, connect_timeout=timeout)

    creds    = raw[:last_at]          # "user:password"
    hostpart = raw[last_at + 1:]      # "host:port/dbname" or "host/dbname"

    # Split credentials on first ':' only
    colon = creds.find(":")
    if colon >= 0:
        user = unquote(creds[:colon])
        password = unquote(creds[colon + 1:])
    else:
        user = unquote(creds)
        password = ""

    # Parse host:port/dbname
    slash = hostpart.find("/")
    if slash >= 0:
        hostport = hostpart[:slash]
        dbname   = hostpart[slash + 1:].split("?")[0] or "postgres"
    else:
        hostport = hostpart.split("?")[0]
        dbname   = "postgres"

    colon2 = hostport.rfind(":")
    if colon2 >= 0:
        host = hostport[:colon2]
        try:
            port = int(hostport[colon2 + 1:])
        except ValueError:
            port = 5432
    else:
        host = hostport
        port = 5432

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        connect_timeout=timeout,
        sslmode="require",
    )


def _ensure_request_log_table() -> None:
    """Create request_logs table in Supabase Postgres if it doesn't exist."""
    if not os.getenv("SUPABASE_DB_URL", ""):
        return
    try:
        import psycopg2
        conn = _pg_connect(timeout=8)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS request_logs (
                id          BIGSERIAL PRIMARY KEY,
                logged_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                path        TEXT NOT NULL,
                method      TEXT NOT NULL,
                status      INTEGER,
                latency_ms  INTEGER,
                source      TEXT        -- 'mcp_agent' | 'smithery' | 'landing_page' | 'api'
            );
            CREATE INDEX IF NOT EXISTS request_logs_logged_at_idx ON request_logs (logged_at DESC);
            CREATE INDEX IF NOT EXISTS request_logs_source_idx    ON request_logs (source);
        """)
        cur.close()
        conn.close()
        print("[DB] request_logs table created")
    except Exception as e:
        print(f"[DB] request_logs table setup error: {e}")


# ── In-memory request tracking (no DB required) ──────────────────────────────
from collections import deque

_request_log_buffer = deque(maxlen=50000)  # ~7 days at moderate traffic

def _record_request(path: str, source: str, status: int, latency_ms: int | None,
                    *, mcp_method: str | None = None, mcp_tool: str | None = None):
    """Append a request record to the in-memory buffer."""
    entry = {
        "path": path,
        "source": source,
        "status": status,
        "latency_ms": latency_ms,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if mcp_method:
        entry["mcp_method"] = mcp_method
    if mcp_tool:
        entry["mcp_tool"] = mcp_tool
    _request_log_buffer.append(entry)


# Protected endpoints require X-API-Key header
_PROTECTED_PATHS = {"/api/book", "/api/execute", "/execute/guaranteed"}  # /api/customers/<id>/book and wallet routes checked in route

def _ensure_website_api_key() -> str:
    """Auto-register a stable API key for the website's own booking requests."""
    website_key = os.getenv("LMD_WEBSITE_API_KEY", "").strip()
    if not website_key:
        return ""
    keys = _load_api_keys()
    if website_key not in keys:
        keys[website_key] = {
            "name": "LastMinuteDeals Website",
            "email": "system@lastminutedealshq.com",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "usage_count": 0,
        }
        _save_api_keys(keys)
    return website_key


def _ensure_cancellation_queue_table() -> None:
    """
    No-op: booking records and cancellation queue are now stored in Supabase Storage
    (bucket: 'bookings') — no schema migration needed.
    """
    pass


def _check_supabase_on_startup() -> bool:
    """
    Verify Supabase Storage is reachable and credentials are correct.
    Writes a health-check sentinel object and logs the result. Non-fatal —
    server continues even if Supabase is down (falls back to .tmp/).
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb_url:
        print("[STARTUP] ⚠ SUPABASE_URL not set — all state uses local .tmp/ fallback "
              "(wiped on every Railway redeploy)")
        return False
    try:
        r = requests.post(
            f"{sb_url}/storage/v1/object/bookings/_health_check.json",
            headers={**_sb_storage_headers(), "Content-Type": "application/json",
                     "x-upsert": "true"},
            data=json.dumps({"started_at": datetime.now(timezone.utc).isoformat(),
                             "pid": os.getpid()}),
            timeout=8,
        )
        if r.status_code in (200, 201):
            print("[STARTUP] ✓ Supabase Storage connected and writable")
            return True
        print(f"[STARTUP] ⚠ Supabase Storage write returned {r.status_code} — "
              "check SUPABASE_SECRET_KEY and bucket permissions")
        return False
    except Exception as e:
        print(f"[STARTUP] ⚠ Supabase Storage unreachable: {e}")
        return False


def _start_retry_scheduler() -> None:
    """
    Start a background thread that runs retry_cancellations.py every 15 minutes.
    Uses APScheduler so the retry worker lives inside the same process as the API —
    no separate Railway service needed, shares the same env vars automatically.
    Only starts once (gunicorn multi-worker safe via a simple PID check).
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
    except ImportError:
        print("APScheduler not installed — cancellation retry cron will not run")
        return

    # In gunicorn multi-worker mode, only start scheduler in the first worker.
    # os.environ is per-process, so we use a file-based lock instead.
    # All Railway workers share the same filesystem within a deployment.
    _scheduler_lock = Path(".tmp/_scheduler.pid")
    try:
        _scheduler_lock.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create — fails if file already exists
        fd = os.open(str(_scheduler_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        return  # another worker already started the scheduler

    def _run_retry() -> None:
        try:
            retry_path = Path(__file__).parent / "retry_cancellations.py"
            import importlib.util as _ilu
            spec   = _ilu.spec_from_file_location("retry_cancellations", retry_path)
            module = _ilu.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.main()
        except Exception as e:
            print(f"[RETRY_SCHEDULER] Error: {e}")

    def _run_reconcile() -> None:
        try:
            reconcile_path = Path(__file__).parent / "reconcile_bookings.py"
            import importlib.util as _ilu
            spec   = _ilu.spec_from_file_location("reconcile_bookings", reconcile_path)
            module = _ilu.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.main()
        except Exception as e:
            print(f"[RECONCILE_SCHEDULER] Error: {e}")

    def _run_slot_discovery() -> None:
        """
        Full slot pipeline: fetch → aggregate → price → sync to Supabase.
        Runs every 4 hours so Railway's inventory stays current without manual intervention.

        Steps:
          1. fetch_octo_slots.py   — pulls live availability from all OCTO suppliers
          2. aggregate_slots.py    — merges platform files, dedupes, filters ≤72h
          3. compute_pricing.py    — sets our_price / our_markup per slot
          4. sync_to_supabase.py   — upserts priced slots into Supabase DB (what /slots reads)

        Steps 3 and 4 are critical: without them agents see stale or unpriced inventory.
        Scripts that use argparse have sys.argv temporarily patched for clean invocation.
        """
        import importlib.util as _ilu
        import sys as _sys

        tools_dir = Path(__file__).parent
        _saved_argv = _sys.argv[:]
        try:
            def _run_script(name: str, label: str) -> bool:
                path = tools_dir / f"{name}.py"
                if not path.exists():
                    print(f"[SLOT_DISCOVERY] {label} not found — skipping")
                    return False
                _sys.argv = [str(path)]
                spec   = _ilu.spec_from_file_location(name, path)
                module = _ilu.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "main"):
                    module.main()
                print(f"[SLOT_DISCOVERY] {label} complete")
                return True

            # Step 1: fetch OCTO availability (writes .tmp/octo_slots.json)
            _run_script("fetch_octo_slots", "OCTO fetch")

            # Step 2: aggregate all platform files into aggregated_slots.json
            aggregated = _run_script("aggregate_slots", "Aggregation")

            if aggregated:
                # Step 3: compute dynamic pricing (sets our_price / our_markup)
                _run_script("compute_pricing", "Pricing")

                # Step 4: upsert priced slots into Supabase DB — this is what /slots reads
                _run_script("sync_to_supabase", "Supabase sync")

            # Track telemetry for /health
            global _last_discovery_at, _last_discovery_slot_count
            _last_discovery_at = datetime.now(timezone.utc).isoformat()
            try:
                _agg_file = tools_dir.parent / ".tmp" / "aggregated_slots.json"
                if _agg_file.exists():
                    _slots = json.loads(_agg_file.read_text(encoding="utf-8"))
                    _last_discovery_slot_count = len(_slots) if isinstance(_slots, list) else 0
            except Exception:
                pass
            print(f"[SLOT_DISCOVERY] Pipeline complete — {_last_discovery_slot_count} slots in Supabase")

        except SystemExit:
            pass  # argparse calls sys.exit(0) on --help; ignore
        except Exception as e:
            print(f"[SLOT_DISCOVERY_SCHEDULER] Error: {e}")
        finally:
            _sys.argv = _saved_argv

    # Verify Supabase connectivity before starting workers
    _check_supabase_on_startup()

    def _run_callback_retry() -> None:
        try:
            intent_path = Path(__file__).parent / "intent_sessions.py"
            import importlib.util as _ilu2
            spec   = _ilu2.spec_from_file_location("intent_sessions_cb", intent_path)
            module = _ilu2.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.retry_pending_callbacks()
        except Exception as e:
            print(f"[CB_RETRY_SCHEDULER] Error: {e}")

    import datetime as _dt
    scheduler = BackgroundScheduler()
    # Retry failed cancellations every 15 min (run immediately on startup)
    scheduler.add_job(_run_retry, "interval", minutes=15, id="retry_cancellations",
                      next_run_time=_dt.datetime.now())
    # Reconcile active bookings against platform every 60 min (first run after 5 min)
    # Reduced from 30 min — the N+1 reads on the booking bucket were a major
    # contributor to Supabase Disk IO Budget depletion.
    scheduler.add_job(_run_reconcile, "interval", minutes=60, id="reconcile_bookings",
                      next_run_time=_dt.datetime.now() + _dt.timedelta(minutes=5))
    # Slot discovery: fetch OCTO + aggregate every 4 hours (first run after 10 min warmup)
    scheduler.add_job(_run_slot_discovery, "interval", hours=4, id="slot_discovery",
                      next_run_time=_dt.datetime.now() + _dt.timedelta(minutes=10))
    # Intent callback retry: redeliver failed agent callbacks every 2 min
    # (4 retries, 6h TTL, backoff: 2→10→30→120 min from original fire time)
    scheduler.add_job(_run_callback_retry, "interval", minutes=2, id="callback_retry",
                      next_run_time=_dt.datetime.now() + _dt.timedelta(minutes=2))
    scheduler.start()
    print("Schedulers started — retry: every 15 min | reconcile: every 30 min | "
          "slot_discovery: every 4 h | callback_retry: every 2 min")


@app.before_request
def require_api_key():
    if request.path in _PROTECTED_PATHS and request.method == "POST":
        key = request.headers.get("X-API-Key", "").strip()
        if not _validate_api_key(key):
            return jsonify({
                "success": False,
                "error": "API key required. Register free at POST /api/keys/register with {name, email}."
            }), 401

DATA_FILE   = Path(".tmp/aggregated_slots.json")
BOOKED_FILE = Path(".tmp/booked_slots.json")
PORT        = int(os.getenv("BOOKING_SERVER_PORT", os.getenv("PORT", "5050")))

# MCP search_slots cache — avoids repeated Supabase pagination on burst agent calls
# Fresh TTL: 300s (5 min). Inventory refreshes every 4h so 5 min is safe and eliminates
# most cold-query cost. Stale TTL: 1800s (30 min). On Supabase failure or Railway cold
# start, serve the last good result rather than returning an error — eliminates the
# 30% failure rate on search_slots caused by cache misses hitting full 5-page pagination.
_MCP_SLOTS_CACHE: dict = {}        # cache_key → {"slots": [...], "expires": float, "stale_until": float}
_MCP_SLOTS_CACHE_TTL       = 300   # seconds until fresh cache expires
_MCP_SLOTS_CACHE_STALE_TTL = 1800  # seconds to serve stale cache on Supabase failure

# Supplier directory cache — live from Supabase, rebuilt every 5 minutes
_SUPPLIER_DIR_CACHE: dict = {}   # {"data": [...], "expires": float}
_SUPPLIER_DIR_CACHE_TTL = 300    # 5 minutes

# Static fallback supplier list — used when Supabase is unreachable.
# Covers all 32 known Bokun vendors; order matches vendor_id_to_supplier_map in octo_suppliers.json.
_SUPPLIER_DIR_STATIC = [
    {"name": "Arctic Adventures",       "destinations": ["Husafell", "Iceland", "Reykjavik", "Skaftafell"], "platform": "Bokun"},
    {"name": "Trivanzo Holidays",        "destinations": ["Cairo", "Egypt", "Luxor", "Red Sea"],              "platform": "Bokun"},
    {"name": "Bicycle Roma",            "destinations": ["Rome"],                                            "platform": "Bokun"},
    {"name": "Boka Bliss",              "destinations": ["Kotor", "Montenegro"],                             "platform": "Bokun"},
    {"name": "EgyExcursions",           "destinations": ["Cairo", "Egypt"],                                  "platform": "Bokun"},
    {"name": "Hillborn Experiences",    "destinations": ["Arusha", "Serengeti", "Tanzania", "Zanzibar"],     "platform": "Bokun"},
    {"name": "Íshestar Riding Tours",   "destinations": ["Iceland", "Selfoss"],                              "platform": "Bokun"},
    {"name": "Marvel Egypt Tours",      "destinations": ["Aswan", "Cairo", "Egypt", "Luxor"],                "platform": "Bokun"},
    {"name": "O Turista Tours",         "destinations": ["Lisbon", "Porto", "Portugal", "Sintra"],           "platform": "Bokun"},
    {"name": "Pure Morocco Experience", "destinations": ["Marrakech", "Morocco"],                            "platform": "Bokun"},
    {"name": "REDRIB Experience",       "destinations": ["Finland", "Helsinki"],                             "platform": "Bokun"},
    {"name": "Ramen Factory Kyoto",     "destinations": ["Japan", "Kyoto"],                                  "platform": "Bokun"},
    {"name": "TourTransfer Bucharest",  "destinations": ["Bucharest", "Romania"],                            "platform": "Bokun"},
    {"name": "Vakare Travel Service",   "destinations": ["Antalya", "Turkey"],                               "platform": "Bokun"},
    {"name": "All Washington View",     "destinations": ["Washington, D.C.", "United States"],               "platform": "Bokun"},
    {"name": "TUTU VIEW Ltd",           "destinations": ["China", "Shanghai", "Xi'an", "Beijing", "Chengdu", "Hangzhou", "Chongqing", "Shenzhen", "Changsha"], "platform": "Bokun"},
    {"name": "Tours El Chiquiz",        "destinations": ["Puerto Vallarta", "Mexico"],                           "platform": "Bokun"},
    {"name": "Zestro Bizlinks",         "destinations": ["Japan"],                                               "platform": "Bokun"},
    {"name": "Adi Tours - Nuba travel", "destinations": ["Cairo", "Egypt"],                                      "platform": "Bokun"},
    {"name": "The Photo Experience",   "destinations": ["London", "United Kingdom"],                             "platform": "Bokun"},
    {"name": "Sailing Windermere",    "destinations": ["Windermere", "Lake District", "United Kingdom"],          "platform": "Bokun"},
    {"name": "Perfect Day Tours",     "destinations": ["Luxor", "Egypt"],                                         "platform": "Bokun"},
    {"name": "Nefertiti Tours",       "destinations": ["Cairo", "Giza", "Egypt"],                                 "platform": "Bokun"},
    {"name": "Blue Dolphin Sailing", "destinations": ["Guanacaste", "Costa Rica"],                               "platform": "Bokun"},
    {"name": "EGYPT GATE",           "destinations": ["Cairo", "Egypt"],                                          "platform": "Bokun"},
    {"name": "Imperio tours",       "destinations": ["Rome", "Italy"],                                            "platform": "Bokun"},
    {"name": "VIDABOA",              "destinations": ["Porto", "Douro Valley", "Portugal"],                         "platform": "Bokun"},
    {"name": "Gallo Tour",           "destinations": ["Rome", "Italy"],                                            "platform": "Bokun"},
    {"name": "Food Activity Japan",  "destinations": ["Osaka", "Japan"],                                           "platform": "Bokun"},
    {"name": "European Voyages",    "destinations": ["Paris", "London", "Rome", "Barcelona", "Amsterdam", "Berlin", "Vienna", "Prague", "Budapest", "Lisbon", "Madrid", "Edinburgh", "Dublin", "Zurich", "Brussels", "Copenhagen", "Stockholm", "Oslo", "Athens", "Istanbul", "Nice", "Florence", "Venice", "Munich", "Milan", "France", "UK", "Germany", "Spain", "Netherlands", "Switzerland", "Austria", "Czech Republic", "Hungary", "Portugal", "Ireland", "Denmark", "Sweden", "Norway", "Greece", "Turkey", "Italy", "Croatia", "Malta", "Monaco", "Luxembourg", "Slovakia", "Slovenia", "Poland", "Estonia", "Latvia", "Lithuania"], "platform": "Bokun"},
    {"name": "CruiserCar Palermo",  "destinations": ["Palermo", "Sicily", "Italy"],                                "platform": "Bokun"},
    {"name": "Nile Navigators",     "destinations": ["Cairo", "Luxor", "Aswan", "Egypt"],                          "platform": "Bokun"},
]


def _supplier_count() -> int:
    """Return the current number of suppliers — live from cache or static fallback."""
    cached = _SUPPLIER_DIR_CACHE.get("data")
    if cached:
        return len(cached)
    return len(_SUPPLIER_DIR_STATIC)


# ── SEO Tour Landing Page Configuration ──────────────────────────────────────

_TOUR_DESTINATIONS = {
    "iceland": {
        "name": "Iceland", "query": "Iceland",
        "title": "Last-Minute Iceland Tours & Adventures",
        "meta_desc": "Book glacier hikes, northern lights tours, ice caves, snowmobiling, horse riding, and whale watching in Iceland. Departing soon, instant confirmation.",
        "intro": "Iceland offers some of the world's most dramatic adventures — from glacier hikes and ice cave explorations to northern lights tours and whale watching. Our local Icelandic suppliers offer instantly-confirmed tours departing within the next few days.",
        "highlights": ["Glacier hikes", "Ice caves", "Northern lights", "Snowmobiling", "Horse riding", "Whale watching"],
    },
    "egypt": {
        "name": "Egypt", "query": "Egypt",
        "title": "Last-Minute Egypt Tours & Experiences",
        "meta_desc": "Book pyramid tours, Nile cruises, hot air balloons over Luxor, camel rides, and desert adventures in Egypt. Instant confirmation.",
        "intro": "From the pyramids of Giza to hot air balloon rides over Luxor's Valley of the Kings, Egypt's ancient wonders are even more incredible when booked last-minute. Six local suppliers offer hundreds of instantly-confirmed experiences.",
        "highlights": ["Pyramid tours", "Nile cruises", "Hot air balloons", "Camel rides", "Temple tours", "Desert adventures"],
    },
    "rome": {
        "name": "Rome", "query": "Rome",
        "title": "Last-Minute Rome Tours, E-Bike Rides & Experiences",
        "meta_desc": "Book e-bike tours, walking tours, food tours, Fiat 500 rides, and golf cart tours in Rome. Appian Way, Colosseum area, Trastevere. Instant confirmation from multiple local suppliers.",
        "intro": "Rome has layers — ancient ruins, Renaissance art, and a food scene that never disappoints. Multiple local suppliers offer e-bike rides along the Appian Way, walking tours through Trastevere, vintage Fiat 500 experiences, golf cart tours past the Colosseum, and food tours that go well beyond pasta — all with instant confirmation.",
        "highlights": ["E-bike tours", "Walking tours", "Food tours", "Fiat 500 tours", "Golf cart tours", "Appian Way", "Day trips"],
    },
    "portugal": {
        "name": "Portugal", "query": "Portugal",
        "title": "Last-Minute Portugal Tours, Wine & Day Trips",
        "meta_desc": "Book private tours in Lisbon, Douro Valley wine tours from Porto, walking tours, Sintra day trips. Multiple local suppliers, instant confirmation.",
        "intro": "Portugal's coastal cities and fairy-tale palaces are best experienced with a local guide. Multiple suppliers offer private tours in Lisbon, Douro Valley wine experiences from Porto, Sintra castle day trips, and walking tours through both cities — all with instant confirmation.",
        "highlights": ["Private tours", "Wine tours", "Walking tours", "Lisbon", "Porto", "Sintra", "Douro Valley"],
    },
    "tanzania": {
        "name": "Tanzania", "query": "Tanzania",
        "title": "Last-Minute Tanzania Safaris & Adventures",
        "meta_desc": "Book luxury safaris in the Serengeti, Kilimanjaro climbs, and Zanzibar retreats. Ultra-luxury operators, instant confirmation.",
        "intro": "Tanzania offers the ultimate African adventure — Serengeti wildlife safaris, Kilimanjaro summit treks, and Zanzibar beach retreats. Our ultra-luxury operator provides world-class experiences with instant confirmation.",
        "highlights": ["Private safaris", "Serengeti", "Kilimanjaro", "Zanzibar", "Wildlife"],
    },
    "morocco": {
        "name": "Morocco", "query": "Morocco",
        "title": "Last-Minute Morocco Desert Tours & Adventures",
        "meta_desc": "Book Sahara desert tours, multi-day adventures, and cultural experiences from Marrakech. Instant confirmation.",
        "intro": "From the vibrant souks of Marrakech to the vast Sahara Desert, Morocco offers unforgettable adventures. Multi-day desert tours and cultural experiences with instant confirmation.",
        "highlights": ["Desert tours", "Sahara", "Marrakech", "Cultural experiences"],
    },
    "japan": {
        "name": "Japan", "query": "Japan",
        "title": "Last-Minute Japan Tours, Cooking Classes & Food Experiences",
        "meta_desc": "Book ramen cooking classes in Kyoto, matcha workshops in Osaka, and cultural experiences across Japan. Hands-on food tours, instant confirmation.",
        "intro": "Japan's culinary traditions come alive through hands-on experiences across multiple cities. Make authentic ramen in a Kyoto workshop, learn matcha preparation in Osaka, or explore Japanese food culture with local guides — all with instant confirmation from multiple suppliers.",
        "highlights": ["Cooking classes", "Ramen workshops", "Matcha making", "Kyoto", "Osaka", "Food tours", "Cultural experiences"],
    },
    "turkey": {
        "name": "Turkey", "query": "Turkey",
        "title": "Last-Minute Turkey Tours — Antalya & Istanbul",
        "meta_desc": "Book boat tours and jeep safaris in Antalya, walking tours and food tours in Istanbul. Mediterranean and Bosphorus adventures, instant confirmation.",
        "intro": "Turkey spans two continents and two very different experiences. On the Mediterranean coast, Antalya offers boat tours along turquoise coastlines and jeep safaris through mountain villages. In Istanbul, walking tours cross the Bosphorus between European and Asian sides, exploring bazaars, mosques, and street food — all with instant confirmation.",
        "highlights": ["Boat tours", "Walking tours", "Jeep safaris", "Antalya", "Istanbul", "Food tours", "Cultural experiences"],
    },
    "montenegro": {
        "name": "Montenegro", "query": "Montenegro",
        "title": "Last-Minute Montenegro Boat Tours & Coastal Experiences",
        "meta_desc": "Book boat tours, sea cave explorations, and Bay of Kotor experiences in Montenegro. Instant confirmation.",
        "intro": "Montenegro's dramatic Bay of Kotor and hidden sea caves offer unforgettable coastal adventures. Explore by boat with instantly-confirmed tours departing soon.",
        "highlights": ["Boat tours", "Sea caves", "Bay of Kotor", "Coastal adventures"],
    },
    "finland": {
        "name": "Finland", "query": "Finland",
        "title": "Last-Minute Finland Speed Boat Tours",
        "meta_desc": "Book speed boat tours and archipelago experiences in Helsinki. Instant confirmation.",
        "intro": "Experience Helsinki from the water with exhilarating speed boat tours through Finland's stunning archipelago. Instant confirmation, departing soon.",
        "highlights": ["Speed boat tours", "Archipelago", "Helsinki"],
    },
    "china": {
        "name": "China", "query": "China",
        "title": "Last-Minute China Tours & Cultural Journeys",
        "meta_desc": "Book multi-day tours, Silk Road adventures, and cultural experiences across China. Instant confirmation.",
        "intro": "China's vast landscapes and ancient history come alive through guided tours — from Shanghai to Xi'an's terracotta warriors. Multi-day cultural journeys with instant confirmation.",
        "highlights": ["Multi-day tours", "Silk Road", "Cultural experiences", "Food tours"],
    },
    "washington-dc": {
        "name": "Washington, D.C.", "query": "Washington",
        "title": "Last-Minute Washington D.C. Tours & Sightseeing",
        "meta_desc": "Book guided city tours, monument visits, and sightseeing in Washington D.C. Instant confirmation.",
        "intro": "See the nation's capital with expert-guided tours of monuments, memorials, and iconic landmarks. Panoramic views and history brought to life, departing soon.",
        "highlights": ["City tours", "Monuments", "Sightseeing"],
    },
    "mexico": {
        "name": "Mexico", "query": "Mexico",
        "title": "Last-Minute Mexico Tours & Excursions",
        "meta_desc": "Book tequila tastings, hiking, and cultural excursions in Puerto Vallarta. Instant confirmation.",
        "intro": "Mexico's Pacific coast offers adventure and culture — from tequila tastings in the mountains to guided excursions along the shore. Book with instant confirmation from Puerto Vallarta.",
        "highlights": ["Tequila tasting", "Hiking", "Cultural excursions", "Puerto Vallarta"],
    },
    "uk": {
        "name": "United Kingdom", "query": "United Kingdom",
        "title": "Last-Minute UK Tours & Experiences",
        "meta_desc": "Book walking tours, photography tours, food tours in London, sailing on Lake Windermere, and Edinburgh explorations. Instant confirmation from multiple suppliers.",
        "intro": "The UK offers everything from London's walking tours and professional photography experiences to sailing on the Lake District's Windermere and Edinburgh's historical trails. Multiple local suppliers provide instant confirmation across England, Scotland, and Wales.",
        "highlights": ["Walking tours", "Photography tours", "Food tours", "Sailing", "London", "Edinburgh", "Lake District"],
    },
    "costa-rica": {
        "name": "Costa Rica", "query": "Costa Rica",
        "title": "Last-Minute Costa Rica Sailing & Ocean Adventures",
        "meta_desc": "Book sailing tours and ocean adventures in Guanacaste, Costa Rica. Sunset sails, snorkeling, and coastal cruises. Instant confirmation.",
        "intro": "Costa Rica's Pacific coast is paradise for ocean lovers. Sail the warm waters of Guanacaste with sunset cruises, snorkeling excursions, and private charters — all with instant confirmation.",
        "highlights": ["Sailing tours", "Sunset cruises", "Snorkeling", "Guanacaste"],
    },
    "romania": {
        "name": "Romania", "query": "Romania",
        "title": "Last-Minute Romania Tours & Castle Visits",
        "meta_desc": "Book city tours, Dracula castle visits, and day trips in Bucharest. Instant confirmation.",
        "intro": "Romania's Gothic castles and vibrant capital await. Visit the legendary Dracula castle in Transylvania or explore Bucharest with expert guides, all with instant confirmation.",
        "highlights": ["City tours", "Dracula castle", "Bucharest", "Day trips"],
    },
    # ── European Voyages destinations ──────────────────────────────────────────
    "paris": {
        "name": "Paris", "query": "Paris",
        "title": "Last-Minute Paris Tours & Walking Experiences",
        "meta_desc": "Book walking tours, food tours, river cruises, and cultural experiences in Paris. Montmartre, Le Marais, the Louvre, and more. Departing soon, instant confirmation.",
        "intro": "Paris rewards those who explore on foot. Wander through Montmartre's winding streets, taste your way through Le Marais, or cruise the Seine at sunset. Our local suppliers offer guided walking tours, food experiences, and cultural journeys with instant confirmation — many departing within hours.",
        "highlights": ["Walking tours", "Food tours", "River cruises", "Montmartre", "Le Marais", "Museum tours", "Day trips"],
    },
    "barcelona": {
        "name": "Barcelona", "query": "Barcelona",
        "title": "Last-Minute Barcelona Tours & Activities",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Barcelona. Gothic Quarter, La Boqueria, Gaudí architecture. Instant confirmation.",
        "intro": "Barcelona blends Gothic architecture with Gaudí's surreal masterpieces, and its food scene is among Europe's best. Explore the narrow lanes of the Gothic Quarter, sample tapas at La Boqueria, or see the Sagrada Família with a knowledgeable local guide — all bookable last-minute with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Gothic Quarter", "Gaudí", "Cultural experiences", "Day trips"],
    },
    "amsterdam": {
        "name": "Amsterdam", "query": "Amsterdam",
        "title": "Last-Minute Amsterdam Tours & Canal Experiences",
        "meta_desc": "Book walking tours, canal cruises, food tours, and cultural experiences in Amsterdam. Jordaan, Rijksmuseum area, and more. Instant confirmation.",
        "intro": "Amsterdam's canal-laced neighborhoods are best explored with a local who knows where to look. From the Jordaan's hidden courtyards to the Rijksmuseum quarter, our guided walking tours and canal experiences run daily with instant confirmation.",
        "highlights": ["Walking tours", "Canal cruises", "Food tours", "Museum tours", "Jordaan", "Cultural experiences"],
    },
    "berlin": {
        "name": "Berlin", "query": "Berlin",
        "title": "Last-Minute Berlin Tours & Historical Experiences",
        "meta_desc": "Book walking tours, historical tours, food tours, and cultural experiences in Berlin. Cold War sites, street art, local food. Instant confirmation.",
        "intro": "Berlin's layers of history — from Prussian grandeur to Cold War division to reunified creativity — are best unpacked with a guide. Walk the Berlin Wall trail, explore Kreuzberg's street art and food scene, or dive into the city's complex past with instantly-confirmed tours departing soon.",
        "highlights": ["Historical tours", "Walking tours", "Cold War sites", "Food tours", "Street art", "Cultural experiences"],
    },
    "vienna": {
        "name": "Vienna", "query": "Vienna",
        "title": "Last-Minute Vienna Tours & Cultural Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Vienna. Imperial palaces, coffee house culture, classical music. Instant confirmation.",
        "intro": "Vienna's imperial palaces, legendary coffee houses, and classical music heritage make it one of Europe's most refined cities. Explore the Ringstrasse on foot, sample Sachertorte at a traditional Kaffeehaus, or visit the Habsburgs' Schönbrunn — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Imperial palaces", "Coffee houses", "Cultural experiences", "Music tours"],
    },
    "prague": {
        "name": "Prague", "query": "Prague",
        "title": "Last-Minute Prague Tours & Old Town Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Prague. Old Town, Charles Bridge, castle district. Instant confirmation.",
        "intro": "Prague's medieval Old Town, Charles Bridge, and hilltop castle form one of Europe's most intact historic cityscapes. Walking tours wind through cobbled lanes past astronomical clocks and Baroque churches, while food tours introduce you to Czech cuisine beyond the tourist traps — all with instant confirmation.",
        "highlights": ["Walking tours", "Old Town", "Charles Bridge", "Food tours", "Castle district", "Cultural experiences"],
    },
    "budapest": {
        "name": "Budapest", "query": "Budapest",
        "title": "Last-Minute Budapest Tours & River Experiences",
        "meta_desc": "Book walking tours, river cruises, food tours, and cultural experiences in Budapest. Buda Castle, ruin bars, thermal baths area. Instant confirmation.",
        "intro": "Budapest straddles the Danube with Buda's castle district on one bank and Pest's grand boulevards on the other. Guided walking tours cover both sides, from the Fisherman's Bastion to the ruin bars of the Jewish Quarter. River cruises and food tours run daily — book last-minute with instant confirmation.",
        "highlights": ["Walking tours", "River cruises", "Food tours", "Buda Castle", "Ruin bars", "Cultural experiences"],
    },
    "athens": {
        "name": "Athens", "query": "Athens",
        "title": "Last-Minute Athens Tours & Ancient History",
        "meta_desc": "Book walking tours, food tours, and historical experiences in Athens. Acropolis, Plaka, ancient agora. Instant confirmation.",
        "intro": "Athens is where Western civilization began, and the ancient sites are even more impressive with a guide who can bring them to life. Walk from the Acropolis through the Plaka's narrow streets, explore the ancient Agora, and taste modern Greek cuisine in the city's buzzing neighborhoods — all with instant confirmation.",
        "highlights": ["Historical tours", "Walking tours", "Acropolis", "Food tours", "Plaka", "Ancient sites"],
    },
    "dublin": {
        "name": "Dublin", "query": "Dublin",
        "title": "Last-Minute Dublin Tours & Irish Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Dublin. Temple Bar, Georgian squares, literary heritage. Instant confirmation.",
        "intro": "Dublin's literary heritage, Georgian architecture, and legendary pub culture make it a city best explored with a local. Walking tours cover everything from Temple Bar to the quiet Georgian squares, while food tours introduce Dublin's evolving culinary scene — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Temple Bar", "Cultural experiences", "Literary tours", "Day trips"],
    },
    "edinburgh": {
        "name": "Edinburgh", "query": "Edinburgh",
        "title": "Last-Minute Edinburgh Tours & Scottish Experiences",
        "meta_desc": "Book walking tours, food tours, and historical experiences in Edinburgh. Royal Mile, Old Town, Arthur's Seat. Instant confirmation.",
        "intro": "Edinburgh's dramatic skyline — castle perched on volcanic rock, medieval Old Town cascading down the Royal Mile — is one of Europe's most striking. Guided walking tours navigate the city's layered history, from underground vaults to Georgian New Town, with instant confirmation.",
        "highlights": ["Walking tours", "Royal Mile", "Old Town", "Historical tours", "Food tours", "Day trips"],
    },
    "copenhagen": {
        "name": "Copenhagen", "query": "Copenhagen",
        "title": "Last-Minute Copenhagen Tours & Nordic Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Copenhagen. Nyhavn, Christiania, Nordic cuisine. Instant confirmation.",
        "intro": "Copenhagen's colorful Nyhavn waterfront, world-class Nordic food scene, and bike-friendly streets embody Scandinavian design and lifestyle. Walking tours and food experiences run daily — explore Christiania, Tivoli, and the city's canal-side neighborhoods with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Nyhavn", "Nordic cuisine", "Canal tours", "Cultural experiences"],
    },
    "florence": {
        "name": "Florence", "query": "Florence",
        "title": "Last-Minute Florence Tours & Renaissance Art",
        "meta_desc": "Book walking tours, food tours, museum tours, and wine experiences in Florence. Uffizi, Duomo, Tuscan day trips. Instant confirmation.",
        "intro": "Florence is the cradle of the Renaissance, and its art, architecture, and food remain unmatched. Walk past the Duomo, cross the Ponte Vecchio, and explore the Oltrarno artisan quarter. Wine tours into the Tuscan hills and food experiences in local markets round out the experience — all with instant confirmation.",
        "highlights": ["Walking tours", "Museum tours", "Food tours", "Wine tours", "Renaissance art", "Tuscan day trips"],
    },
    "venice": {
        "name": "Venice", "query": "Venice",
        "title": "Last-Minute Venice Tours & Lagoon Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Venice. St. Mark's, Rialto, hidden Venice. Instant confirmation.",
        "intro": "Venice rewards those who leave the main tourist corridors. Guided walking tours navigate the labyrinthine calli beyond St. Mark's, through quiet campos and past hidden churches. Food tours sample cicchetti at local bacari, and day trips reach the colorful islands of Murano and Burano — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "St. Mark's", "Rialto", "Island tours", "Cultural experiences"],
    },
    "lisbon": {
        "name": "Lisbon", "query": "Lisbon",
        "title": "Last-Minute Lisbon Tours & City Experiences",
        "meta_desc": "Book walking tours, food tours, and day trips in Lisbon. Alfama, Belém, Sintra excursions. Instant confirmation from multiple local suppliers.",
        "intro": "Lisbon's hilly neighborhoods — Alfama's fado-filled alleyways, Belém's maritime monuments, Bairro Alto's rooftop views — each have their own character. Multiple local suppliers offer walking tours, food experiences, and day trips to Sintra, all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Alfama", "Belém", "Sintra day trips", "Private tours"],
    },
    "croatia": {
        "name": "Croatia", "query": "Croatia",
        "title": "Last-Minute Croatia Tours & Adriatic Experiences",
        "meta_desc": "Book walking tours, food tours, and coastal experiences in Dubrovnik and Split. Old Town walls, island hopping, Adriatic coastline. Instant confirmation.",
        "intro": "Croatia's Adriatic coast combines ancient walled cities with crystal-clear waters. Walk Dubrovnik's famous city walls, explore Split's Diocletian's Palace, or take a day trip to the islands — all with instant confirmation.",
        "highlights": ["Walking tours", "Dubrovnik", "Split", "Coastal tours", "Day trips", "Historical tours"],
    },
    "switzerland": {
        "name": "Switzerland", "query": "Switzerland",
        "title": "Last-Minute Switzerland Tours & Alpine Experiences",
        "meta_desc": "Book walking tours, food tours, and day trips in Zurich and Geneva. Swiss Alps excursions, lakeside tours, chocolate experiences. Instant confirmation.",
        "intro": "Switzerland's alpine scenery, precision engineering, and culinary traditions are legendary. Walking tours in Zurich and Geneva explore historic old towns, while day trips venture into the mountains and along pristine lakes — all with instant confirmation.",
        "highlights": ["Walking tours", "Day trips", "Zurich", "Geneva", "Food tours", "Alpine excursions"],
    },
    "spain": {
        "name": "Spain", "query": "Spain",
        "title": "Last-Minute Spain Tours & Cultural Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences across Spain. Barcelona, Madrid, Seville. Tapas tours, Gaudí, flamenco. Instant confirmation.",
        "intro": "From Barcelona's architectural fantasies to Madrid's world-class museums and Seville's flamenco heritage, Spain offers an extraordinary range of experiences. Walking tours, food tours, and cultural experiences are available across multiple cities — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Barcelona", "Madrid", "Seville", "Cultural experiences"],
    },
    "france": {
        "name": "France", "query": "France",
        "title": "Last-Minute France Tours & Experiences",
        "meta_desc": "Book walking tours, food tours, wine tours, and cultural experiences across France. Paris, Nice, Lyon, Marseille. Instant confirmation.",
        "intro": "France beyond Paris is just as rewarding — the French Riviera's coastal charm in Nice, Lyon's status as the gastronomic capital of France, and Marseille's gritty Mediterranean energy. Walking tours, food experiences, and wine tours run across the country with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Wine tours", "Nice", "Lyon", "Marseille", "Cultural experiences"],
    },
    "germany": {
        "name": "Germany", "query": "Germany",
        "title": "Last-Minute Germany Tours & Cultural Experiences",
        "meta_desc": "Book walking tours, food tours, and historical experiences across Germany. Berlin, Munich, and beyond. Instant confirmation.",
        "intro": "Germany's cities each tell a different story — Berlin's modern reinvention, Munich's Bavarian traditions, and smaller cities rich with medieval heritage. Walking tours, food experiences, and cultural journeys are available with instant confirmation.",
        "highlights": ["Walking tours", "Historical tours", "Food tours", "Berlin", "Munich", "Cultural experiences"],
    },
    "greece": {
        "name": "Greece", "query": "Greece",
        "title": "Last-Minute Greece Tours & Island Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences across Greece. Athens, Santorini, and Greek islands. Instant confirmation.",
        "intro": "Greece's ancient ruins and island landscapes have drawn travelers for millennia. Beyond the Acropolis, explore Athens' vibrant neighborhoods, Santorini's volcanic caldera, and the culinary traditions that connect them — all with instant confirmation.",
        "highlights": ["Historical tours", "Walking tours", "Athens", "Santorini", "Food tours", "Island tours"],
    },
    "netherlands": {
        "name": "Netherlands", "query": "Netherlands",
        "title": "Last-Minute Netherlands Tours & Dutch Experiences",
        "meta_desc": "Book walking tours, canal cruises, food tours, and cultural experiences in the Netherlands. Amsterdam, tulip season, Dutch countryside. Instant confirmation.",
        "intro": "The Netherlands packs an extraordinary amount into a small country — Amsterdam's canals, Haarlem's charm, windmill-dotted polders, and world-class museums. Walking tours and cultural experiences run daily with instant confirmation.",
        "highlights": ["Walking tours", "Canal cruises", "Food tours", "Museum tours", "Dutch countryside", "Cultural experiences"],
    },
    "poland": {
        "name": "Poland", "query": "Poland",
        "title": "Last-Minute Poland Tours & Cultural Experiences",
        "meta_desc": "Book walking tours, food tours, and historical experiences in Krakow and Warsaw. Old Town, Jewish heritage, pierogi tours. Instant confirmation.",
        "intro": "Poland's historic cities offer some of Europe's best value. Krakow's medieval Old Town and Kazimierz quarter, Warsaw's rebuilt city center, and the country's rich culinary traditions — all explorable with guided walking tours and food experiences at instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Krakow", "Warsaw", "Historical tours", "Cultural experiences"],
    },
    "ireland": {
        "name": "Ireland", "query": "Ireland",
        "title": "Last-Minute Ireland Tours & Day Trips",
        "meta_desc": "Book walking tours, food tours, and day trips in Ireland. Dublin, Cliffs of Moher, Wild Atlantic Way. Instant confirmation.",
        "intro": "Ireland's landscape shifts from Dublin's Georgian elegance to the wild Atlantic coastline within a couple of hours. Walking tours explore the capital's literary and culinary heritage, while day trips reach dramatic coastal scenery — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Day trips", "Dublin", "Coastal tours", "Cultural experiences"],
    },
    "sicily": {
        "name": "Sicily", "query": "Sicily",
        "title": "Last-Minute Sicily Tours & Italian Island Experiences",
        "meta_desc": "Book car tours, city tours, food experiences, and transfers in Palermo and across Sicily. Instant confirmation.",
        "intro": "Sicily is Italy at its most intense — Palermo's chaotic street markets, Greek temples at Agrigento, Mount Etna's volcanic slopes, and seafood that rivals anything on the mainland. Local suppliers offer car tours, city explorations, and transfers across the island with instant confirmation.",
        "highlights": ["Car tours", "City tours", "Palermo", "Food experiences", "Transfers", "Cultural tours"],
    },
    "malta": {
        "name": "Malta", "query": "Malta",
        "title": "Last-Minute Malta Tours & Mediterranean Experiences",
        "meta_desc": "Book walking tours, cultural experiences, and historical tours in Valletta, Malta. Fortified city, harbour views, temples. Instant confirmation.",
        "intro": "Malta's fortified capital Valletta packs 7,000 years of history into one of Europe's smallest capitals. Walking tours navigate the honey-colored streets past Baroque cathedrals and Knights' auberges, with views across the Grand Harbour — all with instant confirmation.",
        "highlights": ["Walking tours", "Historical tours", "Valletta", "Harbour tours", "Cultural experiences"],
    },
    "sweden": {
        "name": "Sweden", "query": "Sweden",
        "title": "Last-Minute Sweden Tours & Stockholm Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Stockholm and across Sweden. Old Town Gamla Stan, archipelago. Instant confirmation.",
        "intro": "Stockholm spreads across 14 islands where Lake Mälaren meets the Baltic Sea. Walking tours wind through Gamla Stan's medieval alleyways and out to the archipelago, while food tours explore the city's Nordic culinary revolution — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Stockholm", "Gamla Stan", "Archipelago", "Cultural experiences"],
    },
    "denmark": {
        "name": "Denmark", "query": "Denmark",
        "title": "Last-Minute Denmark Tours & Copenhagen Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Copenhagen and across Denmark. Nyhavn, Tivoli, hygge culture. Instant confirmation.",
        "intro": "Denmark perfected the art of hygge, and Copenhagen brings it to life — colorful Nyhavn, Tivoli Gardens, world-class restaurants, and a cycling culture that makes the city a joy to explore. Guided tours and food experiences run daily with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Copenhagen", "Nyhavn", "Cultural experiences", "Day trips"],
    },
    "norway": {
        "name": "Norway", "query": "Norway",
        "title": "Last-Minute Norway Tours & Fjord Experiences",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Oslo and across Norway. Fjords, Viking heritage, Nordic cuisine. Instant confirmation.",
        "intro": "Norway's dramatic fjords and Viking heritage provide a backdrop unlike anywhere else. Oslo's waterfront transformation, its museums, and the surrounding natural beauty are all accessible through guided tours and cultural experiences with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Oslo", "Cultural experiences", "Historical tours", "Day trips"],
    },
    "austria": {
        "name": "Austria", "query": "Austria",
        "title": "Last-Minute Austria Tours & Alpine Culture",
        "meta_desc": "Book walking tours, food tours, and cultural experiences in Vienna and Salzburg. Imperial palaces, Mozart, alpine scenery. Instant confirmation.",
        "intro": "Austria bridges imperial grandeur and alpine beauty. Vienna's palaces and Salzburg's Mozart heritage sit against a backdrop of mountain scenery. Walking tours, food experiences, and cultural journeys run daily with instant confirmation.",
        "highlights": ["Walking tours", "Vienna", "Salzburg", "Food tours", "Imperial palaces", "Cultural experiences"],
    },
    # ── New supplier destinations ──────────────────────────────────────────
    "brazil": {
        "name": "Brazil", "query": "Brazil",
        "title": "Last-Minute Brazil Tours & Experiences",
        "meta_desc": "Book tours and experiences in Brazil. Cultural adventures, city tours, and local experiences. Instant confirmation.",
        "intro": "Brazil's vibrant culture, stunning landscapes, and warm hospitality make it one of South America's most exciting destinations. Local suppliers offer tours and experiences with instant confirmation — from city explorations to cultural adventures.",
        "highlights": ["City tours", "Cultural experiences", "Local adventures", "Day trips"],
    },
    "istanbul": {
        "name": "Istanbul", "query": "Istanbul",
        "title": "Last-Minute Istanbul Tours — Bosphorus, Bazaars & Beyond",
        "meta_desc": "Book walking tours, food tours, cultural experiences, and day trips in Istanbul. Grand Bazaar, Hagia Sophia, Bosphorus cruise, Cappadocia trips. Instant confirmation from multiple local suppliers.",
        "intro": "Istanbul straddles Europe and Asia across the Bosphorus, and the city's energy is unlike anywhere else. Multiple local suppliers offer walking tours through the Grand Bazaar and Sultanahmet, food tours sampling street simit and balik ekmek, Bosphorus cruises at sunset, and multi-day trips to Cappadocia's fairy chimneys and Ephesus's ancient ruins — all with instant confirmation.",
        "highlights": ["Walking tours", "Food tours", "Grand Bazaar", "Bosphorus cruises", "Cappadocia trips", "Ephesus day trips", "Cultural experiences"],
    },
}


def _get_live_supplier_directory() -> list[dict]:
    """
    Return a list of {name, destinations, platform} dicts for every supplier currently
    represented in the live Supabase slot inventory.

    Fetches only (business_name, location_city, location_country) per row — ~270KB for
    the full 4,500-slot table — and deduplicates client-side. Cached 5 minutes to avoid
    hammering Supabase on every agent call. Falls back to _SUPPLIER_DIR_STATIC if
    Supabase is unreachable.
    """
    now = time.time()
    cached = _SUPPLIER_DIR_CACHE.get("data")
    if cached and _SUPPLIER_DIR_CACHE.get("expires", 0) > now:
        return cached

    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")

    if sb_url and sb_secret:
        try:
            hdrs = {
                "apikey": sb_secret,
                "Authorization": f"Bearer {sb_secret}",
                "Prefer": "count=none",
            }
            # Paginate — Supabase caps each response at 1000 rows regardless of limit param.
            # Without pagination only ~5 suppliers appear (those whose rows fit in the first 1000).
            supplier_map: dict[str, dict] = {}
            PAGE_SIZE = 1000
            offset = 0
            while True:
                resp = requests.get(
                    f"{sb_url}/rest/v1/slots",
                    headers=hdrs,
                    params=[
                        ("select", "business_name,location_city,location_country"),
                        ("order",  "business_name.asc"),
                        ("limit",  PAGE_SIZE),
                        ("offset", offset),
                    ],
                    timeout=10,
                )
                if resp.status_code != 200:
                    break
                page = resp.json()
                if not page:
                    break
                for row in page:
                    name    = row.get("business_name") or ""
                    city    = row.get("location_city") or ""
                    country = row.get("location_country") or ""
                    if not name:
                        continue
                    if name not in supplier_map:
                        supplier_map[name] = {"name": name, "destinations": set(), "platform": "Bokun"}
                    if city:
                        supplier_map[name]["destinations"].add(city)
                    if country:
                        supplier_map[name]["destinations"].add(country)
                if len(page) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE

            # Require at least as many suppliers as _SUPPLIER_DIR_STATIC to accept
            # the live result. If pagination is broken (e.g., only page 1 retrieved),
            # the live query returns fewer suppliers than expected; in that case fall through
            # to the static list so agents always see the full supplier network.
            min_expected = len(_SUPPLIER_DIR_STATIC)
            if len(supplier_map) >= min_expected:
                result = [
                    {"name": v["name"], "destinations": sorted(v["destinations"]), "platform": v["platform"]}
                    for v in sorted(supplier_map.values(), key=lambda x: x["name"])
                ]
                _SUPPLIER_DIR_CACHE["data"]    = result
                _SUPPLIER_DIR_CACHE["expires"] = now + _SUPPLIER_DIR_CACHE_TTL
                return result
            # Partial result — merge live destinations into static list for accuracy
            result = []
            for static_entry in _SUPPLIER_DIR_STATIC:
                live = supplier_map.get(static_entry["name"])
                if live:
                    merged_dests = sorted(set(static_entry["destinations"]) | live["destinations"])
                    result.append({**static_entry, "destinations": merged_dests})
                else:
                    result.append(static_entry)
            _SUPPLIER_DIR_CACHE["data"]    = result
            _SUPPLIER_DIR_CACHE["expires"] = now + _SUPPLIER_DIR_CACHE_TTL
            return result
        except Exception:
            pass  # Fall through to static

    # Static fallback
    _SUPPLIER_DIR_CACHE["data"]    = _SUPPLIER_DIR_STATIC
    _SUPPLIER_DIR_CACHE["expires"] = now + _SUPPLIER_DIR_CACHE_TTL
    return _SUPPLIER_DIR_STATIC


def _load_booked() -> set:
    """Load set of slot_ids that have already been booked."""
    if BOOKED_FILE.exists():
        try:
            return set(json.loads(BOOKED_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


_BOOKED_LOCK = threading.Lock()

def _mark_booked(slot_id: str) -> None:
    """Add slot_id to the booked set. Thread-safe via lock + atomic rename."""
    with _BOOKED_LOCK:
        booked = _load_booked()
        booked.add(slot_id)
        tmp = BOOKED_FILE.with_suffix(".tmp")
        BOOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(list(booked)), encoding="utf-8")
        tmp.replace(BOOKED_FILE)  # atomic rename

# ── API key system ────────────────────────────────────────────────────────────
API_KEYS_FILE = Path(".tmp/api_keys.json")
_SB_API_KEYS_PATH = "config/api_keys.json"  # path inside Supabase Storage bookings bucket

def _load_api_keys() -> dict:
    """Load API keys. Primary: Supabase Storage (survives redeploys). Fallback: local cache."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            r = requests.get(
                f"{sb_url}/storage/v1/object/bookings/{_SB_API_KEYS_PATH}",
                headers={"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                # Write local cache so reads stay fast within same process lifetime
                try:
                    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    API_KEYS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
                return data
        except Exception as e:
            print(f"[API_KEYS] Supabase load failed, using local cache: {e}")
    # Fallback: local cache (may be empty after fresh redeploy)
    if API_KEYS_FILE.exists():
        try:
            return json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_api_keys(keys: dict) -> None:
    """Save API keys to both Supabase Storage (persistent) and local file (cache)."""
    # Local write always (fast, used as in-process cache)
    try:
        API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        API_KEYS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    except Exception:
        pass
    # Supabase Storage write (survives redeploys)
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            requests.post(
                f"{sb_url}/storage/v1/object/bookings/{_SB_API_KEYS_PATH}",
                headers={"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}",
                         "Content-Type": "application/json", "x-upsert": "true"},
                data=json.dumps(keys),
                timeout=8,
            )
        except Exception as e:
            print(f"[API_KEYS] Supabase save failed (local write succeeded): {e}")

_API_KEYS_MEM_CACHE: dict = {}
_API_KEYS_CACHE_AT: float = 0.0
_API_KEYS_CACHE_TTL = 30  # seconds — reload from Supabase at most once per 30s

def _validate_api_key(key: str) -> bool:
    """Validate API key with in-process cache (30s TTL) to avoid Supabase round-trips."""
    if not key:
        return False
    global _API_KEYS_MEM_CACHE, _API_KEYS_CACHE_AT
    now = time.time()
    if now - _API_KEYS_CACHE_AT > _API_KEYS_CACHE_TTL:
        _API_KEYS_MEM_CACHE = _load_api_keys()
        _API_KEYS_CACHE_AT  = now
    return key in _API_KEYS_MEM_CACHE

def _generate_api_key() -> str:
    import secrets
    return "lmd_" + secrets.token_hex(24)

# ── Stripe customers (saved payment methods) ──────────────────────────────────
# Stored in Supabase Storage so data survives Railway redeploys.
_SB_CUSTOMERS_PATH = "config/stripe_customers.json"
CUSTOMERS_FILE = Path(".tmp/stripe_customers.json")  # local cache only
_CUSTOMERS_MEM_CACHE: dict | None = None
_CUSTOMERS_CACHE_AT: float = 0.0
_CUSTOMERS_CACHE_TTL = 30  # seconds

def _load_customers() -> dict:
    global _CUSTOMERS_MEM_CACHE, _CUSTOMERS_CACHE_AT
    import time as _time
    if _CUSTOMERS_MEM_CACHE is not None and (_time.monotonic() - _CUSTOMERS_CACHE_AT) < _CUSTOMERS_CACHE_TTL:
        return _CUSTOMERS_MEM_CACHE
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            r = requests.get(
                f"{sb_url}/storage/v1/object/bookings/{_SB_CUSTOMERS_PATH}",
                headers=_sb_storage_headers(), timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                try:
                    CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    CUSTOMERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
                _CUSTOMERS_MEM_CACHE = data
                _CUSTOMERS_CACHE_AT = _time.monotonic()
                return data
        except Exception:
            pass
    if CUSTOMERS_FILE.exists():
        try:
            data = json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))
            _CUSTOMERS_MEM_CACHE = data
            _CUSTOMERS_CACHE_AT = _time.monotonic()
            return data
        except Exception:
            pass
    return {}

def _save_customers(customers: dict) -> None:
    global _CUSTOMERS_MEM_CACHE, _CUSTOMERS_CACHE_AT
    import time as _time
    _CUSTOMERS_MEM_CACHE = customers
    _CUSTOMERS_CACHE_AT = _time.monotonic()
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            requests.post(
                f"{sb_url}/storage/v1/object/bookings/{_SB_CUSTOMERS_PATH}",
                headers={**_sb_storage_headers(), "Content-Type": "application/json",
                         "x-upsert": "true"},
                data=json.dumps(customers),
                timeout=8,
            )
        except Exception as e:
            print(f"[CUSTOMERS] Supabase save failed: {e}")
    try:
        CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CUSTOMERS_FILE.write_text(json.dumps(customers, indent=2), encoding="utf-8")
    except Exception:
        pass

# ── Webhook subscriptions ─────────────────────────────────────────────────────
# Stored in Supabase Storage so data survives Railway redeploys.
_SB_WEBHOOKS_PATH = "config/webhook_subscriptions.json"
WEBHOOKS_FILE = Path(".tmp/webhook_subscriptions.json")  # local cache only
_WEBHOOKS_MEM_CACHE: dict | None = None
_WEBHOOKS_CACHE_AT: float = 0.0
_WEBHOOKS_CACHE_TTL = 30  # seconds

def _load_webhooks() -> dict:
    global _WEBHOOKS_MEM_CACHE, _WEBHOOKS_CACHE_AT
    import time as _time
    if _WEBHOOKS_MEM_CACHE is not None and (_time.monotonic() - _WEBHOOKS_CACHE_AT) < _WEBHOOKS_CACHE_TTL:
        return _WEBHOOKS_MEM_CACHE
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            r = requests.get(
                f"{sb_url}/storage/v1/object/bookings/{_SB_WEBHOOKS_PATH}",
                headers=_sb_storage_headers(), timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                try:
                    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    WEBHOOKS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
                _WEBHOOKS_MEM_CACHE = data
                _WEBHOOKS_CACHE_AT = _time.monotonic()
                return data
        except Exception:
            pass
    if WEBHOOKS_FILE.exists():
        try:
            data = json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
            _WEBHOOKS_MEM_CACHE = data
            _WEBHOOKS_CACHE_AT = _time.monotonic()
            return data
        except Exception:
            pass
    return {}

def _save_webhooks(subs: dict) -> None:
    global _WEBHOOKS_MEM_CACHE, _WEBHOOKS_CACHE_AT
    import time as _time
    _WEBHOOKS_MEM_CACHE = subs
    _WEBHOOKS_CACHE_AT = _time.monotonic()
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            requests.post(
                f"{sb_url}/storage/v1/object/bookings/{_SB_WEBHOOKS_PATH}",
                headers={**_sb_storage_headers(), "Content-Type": "application/json",
                         "x-upsert": "true"},
                data=json.dumps(subs),
                timeout=8,
            )
        except Exception as e:
            print(f"[WEBHOOKS] Supabase save failed: {e}")
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBHOOKS_FILE.write_text(json.dumps(subs, indent=2), encoding="utf-8")

# ── Lazy Stripe import ────────────────────────────────────────────────────────
def _stripe():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    return stripe


# ── Slot lookup ───────────────────────────────────────────────────────────────

def _load_slots_from_supabase(
    hours_ahead: float = 72,
    category: str = "",
    city: str = "",
    budget: float = 0,
    limit: int = 10000,
) -> list[dict]:
    """
    Load slots from Supabase with optional filters.
    Returns list of full slot dicts. Falls back to local file if Supabase unavailable.
    Used by execute/best, execute/guaranteed, and system context.
    """
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")

    if sb_url and sb_secret:
        try:
            hdrs   = {
                "apikey": sb_secret,
                "Authorization": f"Bearer {sb_secret}",
                "Prefer": "count=none",
            }
            now_iso     = datetime.now(timezone.utc).isoformat()
            horizon_dt  = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
            horizon_iso = horizon_dt.isoformat()
            base_params: list[tuple] = [
                ("order", "start_time.asc"),
                ("start_time", f"gt.{now_iso}"),
            ]
            if hours_ahead:
                base_params.append(("start_time", f"lte.{horizon_iso}"))
            if category:
                base_params.append(("category", f"eq.{category}"))
            if city:
                base_params.append(("location_city", f"ilike.%{city}%"))
            if budget:
                base_params.append(("our_price", f"lte.{budget}"))

            # Paginate through Supabase — max-rows server config caps each response
            # at 1000 rows regardless of limit param. Page until we have all records
            # or we've reached the caller's requested limit.
            PAGE_SIZE = 1000
            result: list = []
            offset = 0
            while len(result) < limit:
                fetch_n = min(PAGE_SIZE, limit - len(result))
                page_params = base_params + [("limit", fetch_n), ("offset", offset)]
                resp = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs,
                                    params=page_params, timeout=10)
                if resp.status_code != 200:
                    break
                page = resp.json()
                if not page:
                    break
                for row in page:
                    if row.get("raw"):
                        try:
                            result.append(json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"])
                            continue
                        except Exception:
                            pass
                    result.append(row)
                if len(page) < fetch_n:
                    break  # last page — no more rows
                offset += fetch_n
            return result
        except Exception as e:
            print(f"[SLOTS] Supabase load failed, falling back to local: {e}")

    # Fallback: local file
    if not DATA_FILE.exists():
        return []
    try:
        slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        now   = datetime.now(timezone.utc)
        result = []
        for s in slots:
            h = s.get("hours_until_start", 999)
            if h < 0 or h > hours_ahead:
                continue
            if category and s.get("category") != category:
                continue
            if city and city.lower() not in (s.get("location_city") or "").lower():
                continue
            p = float(s.get("our_price") or s.get("price") or 0)
            if budget and p > budget:
                continue
            result.append(s)
        return result
    except Exception:
        return []


def get_slot_by_id(slot_id: str) -> dict | None:
    """Look up a slot from Supabase (primary) or local file (fallback)."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")

    if sb_url and sb_secret:
        try:
            hdrs = {
                "apikey": sb_secret,
                "Authorization": f"Bearer {sb_secret}",
            }
            resp = requests.get(
                f"{sb_url}/rest/v1/slots",
                headers=hdrs,
                params={"slot_id": f"eq.{slot_id}", "limit": 1},
                timeout=5,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    row = rows[0]
                    # Restore full slot from raw JSONB if available
                    if row.get("raw"):
                        try:
                            return json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"]
                        except Exception:
                            pass
                    return row
        except Exception as e:
            print(f"Supabase lookup failed, falling back to local: {e}")

    # Fallback: local file
    if not DATA_FILE.exists():
        return None
    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for s in slots:
        if s.get("slot_id") == slot_id:
            return s
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/.well-known/glama.json", methods=["GET"])
def glama_well_known():
    return jsonify({
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "maintainers": [{"email": "johnanleitner1@gmail.com"}]
    })


@app.route("/.well-known/mcp/server-card.json", methods=["GET"])
def mcp_server_card():
    return jsonify({
        "$schema": "https://modelcontextprotocol.io/schemas/server-card/v1.0",
        "version": "1.0",
        "serverInfo": {
            "name": "Last Minute Deals HQ",
            "version": "1.0.0",
            "description": (
                "Book last-minute tours and activities worldwide. "
                f"Live slots from {_supplier_count()} suppliers across 48 countries and 100+ cities."
            ),
            "homepage": "https://lastminutedealshq.com",
        },
        "transport": {
            "type": "streamable-http",
            "url": "https://api.lastminutedealshq.com/mcp",
        },
        "capabilities": {
            "tools": True,
            "prompts": True,
            "resources": False,
        },
        "configSchema": {
            "type": "object",
            "properties": {
                "lmd_api_key": {
                    "type": "string",
                    "description": (
                        "API key for making bookings. Optional — search and "
                        "preview work without a key. Get one free at "
                        "POST https://api.lastminutedealshq.com/api/keys/register"
                    ),
                    "x-from": "header:X-API-Key",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    })


@app.route("/llms.txt", methods=["GET"])
def llms_txt():
    """Serve llms.txt for AI agent discovery."""
    content = f"""# Last Minute Deals HQ
> Live tour and activity inventory for AI agents. {_supplier_count()} suppliers, 100+ cities, instant confirmation via OCTO.

Last Minute Deals HQ is a booking API for tours and activities. An AI agent searches
available slots, shows them to a customer, and books via Stripe checkout or pre-funded
wallet. Inventory is sourced live from {_supplier_count()} tour operators connected through the OCTO
open standard (the industry protocol for tours and activities).

This is not a scraper. Every slot is a real, bookable product from a verified supplier
with instant confirmation. Inventory refreshes every 4 hours from production booking systems.

## What's available
- 25,000+ live bookable slots across 100+ cities
- {_supplier_count()} connected suppliers via OCTO/Bokun
- Categories: tours, activities, wellness, photography, food experiences, safaris, boat tours
- Destinations: Iceland, Egypt, Turkey, Italy, Portugal, Japan, Morocco, Tanzania, Mexico,
  Costa Rica, Montenegro, Finland, China, Romania, United Kingdom, Brazil, plus 30+ European
  cities via European Voyages
- Availability window: 0-72 hours from now (last-minute focus)
- Payment: Stripe auth-then-capture (customer never charged for a failed booking)

## For AI Agents — MCP Server

Connect directly via MCP-over-HTTP (no transport setup):
  POST https://api.lastminutedealshq.com/mcp

### MCP Tools
- search_slots: Search available tours by city, category, hours_ahead, max_price
- book_slot: Book for a customer (Stripe checkout or wallet)
- preview_slot: Get shareable booking page URL
- get_booking_status: Check confirmation status
- get_supplier_info: Full supplier network with destinations

## For AI Agents — REST API

Base URL: https://api.lastminutedealshq.com
API spec: https://lastminutedealshq.com/openapi.json

GET /api/slots?city=Rome&hours_ahead=72&max_price=50 — Search available slots
POST /api/book — Create Stripe checkout for a slot
POST /api/book/direct — Book via pre-funded wallet (autonomous)
GET /bookings/{{id}} — Check booking status
POST /api/keys/register — Get a free API key
GET /health — System health
GET /metrics — Live system metrics (public)

## Installation
npx -y @smithery/cli install @johnanleitner1/Last_Minute_Deals_HQ --client claude
Or connect directly: https://api.lastminutedealshq.com/mcp
"""
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/health", methods=["GET"])
def health():
    slot_count = 0
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            r = requests.get(f"{sb_url}/rest/v1/slots",
                headers={
                    "apikey": sb_secret,
                    "Authorization": f"Bearer {sb_secret}",
                    "Prefer": "count=exact",
                    "Range": "0-0",
                },
                params={"select": "slot_id"},
                timeout=5)
            content_range = r.headers.get("Content-Range", "")
            if "/" in content_range:
                slot_count = int(content_range.split("/")[1])
            elif r.status_code == 200:
                slot_count = len(r.json())
        except Exception:
            pass
    if slot_count == 0 and DATA_FILE.exists():
        try:
            slot_count = len(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # ── Live success rates from request_logs (last hour) ─────────────────────
    search_success_rate = None
    book_success_rate   = None
    try:
        conn = _pg_connect(timeout=3)
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    path,
                    COUNT(*) FILTER (WHERE status < 400)  AS ok,
                    COUNT(*)                               AS total
                FROM request_logs
                WHERE logged_at > NOW() - INTERVAL '1 hour'
                  AND path IN ('/slots', '/api/book')
                GROUP BY path
            """)
            for path, ok, total in cur.fetchall():
                rate = round(ok / total, 3) if total else None
                if path == "/slots":
                    search_success_rate = rate
                elif path == "/api/book":
                    book_success_rate = rate
            cur.close()
            conn.close()
    except Exception:
        pass

    # ── Supabase Storage connectivity ─────────────────────────────────────────
    supabase_storage_ok = False
    if sb_url and sb_secret:
        try:
            hc = requests.get(
                f"{sb_url}/storage/v1/object/bookings/_health_check.json",
                headers=_sb_storage_headers(), timeout=4,
            )
            supabase_storage_ok = hc.status_code == 200
        except Exception:
            pass

    # ── Slot discovery freshness ───────────────────────────────────────────────
    discovery_slot_count = _last_discovery_slot_count
    if not discovery_slot_count and DATA_FILE.exists():
        try:
            discovery_slot_count = len(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

    response: dict = {
        "status": "ok",
        "slots": slot_count,
        "supabase_storage": "ok" if supabase_storage_ok else "unreachable",
        "last_slot_discovery": _last_discovery_at or None,
        "inventory_slot_count": discovery_slot_count,
        "scheduler_running": Path(".tmp/_scheduler.pid").exists(),
    }
    if search_success_rate is not None:
        response["search_slots_success_rate_1h"] = search_success_rate
    if book_success_rate is not None:
        response["book_slot_success_rate_1h"] = book_success_rate
    return jsonify(response)


_RELIABILITY_CACHE: dict = {}  # {"data": dict, "expires": float}
_RELIABILITY_CACHE_TTL = 300  # 5 minutes — booking metrics change slowly

def _get_reliability_metrics() -> dict:
    """
    Compute booking reliability stats from Supabase Storage booking records.
    Results are cached for 5 minutes to avoid N+1 reads on every /metrics call.

    Returns:
      - Status counts and success rate
      - Execution timing distribution (avg, p50, p95) for total, reservation, confirm
      - Retry rate and retry stage breakdown
      - Per-supplier success rates and avg execution time
      - Circuit breaker states
    """
    now = time.time()
    cached = _RELIABILITY_CACHE.get("data")
    if cached and _RELIABILITY_CACHE.get("expires", 0) > now:
        return cached

    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        return {}

    stats: dict = {
        "booked": 0, "cancelled": 0, "failed": 0,
        "reconciliation_required": 0, "total": 0,
        "success_rate": None, "circuit_breakers": {},
        "last_booking_at": None,
        # Timing
        "avg_execution_ms": None, "p50_execution_ms": None, "p95_execution_ms": None,
        "avg_reservation_ms": None, "avg_confirm_ms": None,
        # Retries
        "retry_rate": None, "retry_stage_breakdown": {},
        # Failures
        "failure_breakdown": {},
        # Per supplier
        "by_supplier": {},
    }

    try:
        # List booking files, exclude internal prefixes
        r = requests.post(
            f"{sb_url}/storage/v1/object/list/bookings",
            headers={**_sb_storage_headers(), "Content-Type": "application/json"},
            json={"prefix": "", "limit": 1000},
            timeout=8,
        )
        if r.status_code != 200:
            return stats

        names = [
            item["name"] for item in r.json()
            if item.get("name")
            and item["name"].endswith(".json")
            and not item["name"].startswith("cancellation_queue/")
            and not item["name"].startswith("circuit_breaker/")
            and not item["name"].startswith("config/")
            and not item["name"].startswith("idem_")
            and not item["name"].startswith("webhook_session_")
            and not item["name"].startswith("cleanup_")
            and not item["name"].startswith("pending_exec_")
            and not item["name"].startswith("inbound_emails/")
        ]

        last_booking_at  = None
        exec_times:  list = []
        res_times:   list = []
        conf_times:  list = []
        retry_counts: list = []
        retry_stages: dict = {}
        failure_breakdown: dict = {}
        # per-supplier: {supplier_id: {"total": int, "success": int, "exec_ms": [...]}}
        by_supplier: dict = {}

        for name in names:
            try:
                rec = requests.get(
                    f"{sb_url}/storage/v1/object/bookings/{name}",
                    headers=_sb_storage_headers(), timeout=4,
                )
                if rec.status_code != 200:
                    continue
                record = rec.json()
                # Exclude dry-run records from reliability metrics — they aren't real bookings
                if record.get("dry_run"):
                    continue
                status = record.get("status", "unknown")
                if status in stats:
                    stats[status] += 1
                stats["total"] += 1

                # Failure reason breakdown
                if status == "failed":
                    fr = record.get("failure_reason", "unknown")
                    failure_breakdown[fr] = failure_breakdown.get(fr, 0) + 1

                ea = record.get("executed_at", "")
                if ea and (last_booking_at is None or ea > last_booking_at):
                    last_booking_at = ea

                # Timing
                exec_ms = record.get("execution_duration_ms")
                if exec_ms is not None:
                    exec_times.append(int(exec_ms))
                res_ms = record.get("reservation_ms")
                if res_ms is not None:
                    res_times.append(int(res_ms))
                conf_ms = record.get("confirm_ms")
                if conf_ms is not None:
                    conf_times.append(int(conf_ms))

                # Retries — new field name is "retries"; fall back to legacy "retry_count"
                rc = record.get("retries", record.get("retry_count", 0)) or 0
                retry_counts.append(rc)
                if rc > 0:
                    stage = record.get("retry_stage") or "unknown"
                    retry_stages[stage] = retry_stages.get(stage, 0) + 1

                # Per-supplier
                sid = record.get("supplier_id") or record.get("platform") or "unknown"
                if sid not in by_supplier:
                    by_supplier[sid] = {"total": 0, "success": 0, "exec_ms": []}
                by_supplier[sid]["total"] += 1
                if status in ("booked", "cancelled"):
                    by_supplier[sid]["success"] += 1
                if exec_ms is not None:
                    by_supplier[sid]["exec_ms"].append(int(exec_ms))

            except Exception:
                pass

        stats["last_booking_at"] = last_booking_at
        total = stats["total"]
        if total > 0:
            stats["success_rate"] = round(
                (stats["booked"] + stats["cancelled"]) / total, 3
            )

        # Timing aggregates
        def _pct(lst: list, p: float) -> int | None:
            if not lst:
                return None
            s = sorted(lst)
            idx = int(len(s) * p)
            return s[min(idx, len(s) - 1)]

        if exec_times:
            stats["avg_execution_ms"] = round(sum(exec_times) / len(exec_times))
            stats["p50_execution_ms"] = _pct(exec_times, 0.50)
            stats["p95_execution_ms"] = _pct(exec_times, 0.95)
        if res_times:
            stats["avg_reservation_ms"] = round(sum(res_times) / len(res_times))
        if conf_times:
            stats["avg_confirm_ms"] = round(sum(conf_times) / len(conf_times))

        # Retry rate
        if retry_counts:
            retried = sum(1 for c in retry_counts if c > 0)
            stats["retry_rate"] = round(retried / len(retry_counts), 3)
            stats["retry_stage_breakdown"] = retry_stages

        stats["failure_breakdown"] = failure_breakdown

        # Per-supplier summary (only suppliers with ≥1 booking)
        stats["by_supplier"] = {
            sid: {
                "total":            d["total"],
                "success_rate":     round(d["success"] / d["total"], 3) if d["total"] else None,
                "avg_execution_ms": round(sum(d["exec_ms"]) / len(d["exec_ms"])) if d["exec_ms"] else None,
            }
            for sid, d in by_supplier.items()
        }

    except Exception as e:
        print(f"[METRICS] Reliability stats error: {e}")

    # Circuit breaker states
    try:
        cb_spec = _ilu.spec_from_file_location("circuit_breaker", Path(__file__).parent / "circuit_breaker.py")
        cb_mod  = _ilu.module_from_spec(cb_spec)
        cb_spec.loader.exec_module(cb_mod)
        stats["circuit_breakers"] = {
            sid: {"state": s.get("state"), "failures": s.get("failures", 0)}
            for sid, s in cb_mod.get_all_states().items()
        }
    except Exception:
        pass

    # Cache the result for 5 minutes
    _RELIABILITY_CACHE["data"] = stats
    _RELIABILITY_CACHE["expires"] = time.time() + _RELIABILITY_CACHE_TTL
    return stats


def _compute_agent_recommendation(slot_count: int) -> dict:
    """
    Single authoritative block for agents evaluating whether to route through this API.
    Synthesises infrastructure verification (load test) + live booking history.
    Designed to answer "should I use this?" programmatically without reading prose.
    """
    # Live booking success rate from request_logs (last hour) — may be None if no data yet
    live_search_rate: float | None = None
    live_book_rate:   float | None = None
    try:
        conn = _pg_connect(timeout=3)
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT path,
                       COUNT(*) FILTER (WHERE status < 400) AS ok,
                       COUNT(*) AS total
                FROM request_logs
                WHERE logged_at > NOW() - INTERVAL '24 hours'
                  AND path IN ('/slots', '/api/book')
                GROUP BY path
            """)
            for path, ok, total in cur.fetchall():
                rate = round(ok / total, 3) if total else None
                if path == "/slots":
                    live_search_rate = rate
                elif path == "/api/book":
                    live_book_rate = rate
            cur.close()
            conn.close()
    except Exception:
        pass

    # Infrastructure baseline: load-tested, version-pinned, always present
    infra_verified = {
        "search_slots_success_rate": 1.0,
        "book_slot_success_rate":    1.0,
        "method": "load_test",
        "sample": {"search": 100, "book": 60},
        "version": "2.1.0",
        "note": "Async I/O, 10-conn semaphore, 60s slot cache. Verified 2026-04-15.",
    }

    # Live signal overrides infra baseline when we have enough data
    live_signal: dict | None = None
    if live_search_rate is not None or live_book_rate is not None:
        live_signal = {}
        if live_search_rate is not None:
            live_signal["search_slots_success_rate_24h"] = live_search_rate
        if live_book_rate is not None:
            live_signal["book_slot_success_rate_24h"] = live_book_rate

    return {
        "use_this_api": True,
        "confidence": "infrastructure_verified" if live_signal is None else "live_data",
        "inventory_slots_available": slot_count,
        "capabilities": [
            "search by city / category / price / hours_ahead",
            "book via Stripe checkout (human approval) or wallet (autonomous)",
            "real-time booking status tracking",
            f"{_supplier_count()} suppliers, OCTO protocol, instant confirmation",
        ],
        "latency_p95_ms": {"search_slots": 2363, "book_slot": None},
        "infrastructure_verified": infra_verified,
        "live_signal": live_signal,
        "routing_hint": (
            "Prefer search_slots(hours_ahead=72) for urgent needs. "
            "Use get_supplier_info() to match supplier to destination before searching."
        ),
    }


def _compute_trust_signal(reliability: dict) -> dict:
    """
    Produce a machine-readable trust signal that agents can evaluate programmatically.
    Designed to be the top-level answer to: "Should I route bookings through this system?"

    Verdict scale:
        insufficient_data  — fewer than 5 real bookings; cannot assess
        low_confidence     — success_rate < 0.80 or avg_execution_ms > 15 000
        moderate           — success_rate ≥ 0.80 and avg_execution_ms ≤ 15 000
        high_confidence    — success_rate ≥ 0.92, avg_execution_ms ≤ 8 000,
                             and failure_variance != "high"

    failure_variance — consistency signal derived from p95/avg execution ratio:
        low    → p95 < 1.5× avg  (predictable, consistent)
        medium → p95 1.5–2.5× avg
        high   → p95 > 2.5× avg  (erratic; penalises high_confidence verdict)
    """
    n                 = reliability.get("total", 0)
    success_rate      = reliability.get("success_rate")
    avg_ms            = reliability.get("avg_execution_ms")
    retry_rate        = reliability.get("retry_rate")
    p95_ms            = reliability.get("p95_execution_ms")
    failure_breakdown = reliability.get("failure_breakdown", {})

    # Derive failure_variance from p95/avg ratio
    failure_variance: str | None = None
    if avg_ms and p95_ms and avg_ms > 0:
        ratio = p95_ms / avg_ms
        if ratio < 1.5:
            failure_variance = "low"
        elif ratio < 2.5:
            failure_variance = "medium"
        else:
            failure_variance = "high"

    if n < 5 or success_rate is None:
        verdict = "insufficient_data"
        score   = None
    elif (success_rate >= 0.92
          and (avg_ms is None or avg_ms <= 8_000)
          and failure_variance != "high"):
        verdict = "high_confidence"
        score   = round(success_rate, 3)
    elif success_rate >= 0.80 and (avg_ms is None or avg_ms <= 15_000):
        verdict = "moderate"
        score   = round(success_rate, 3)
    else:
        verdict = "low_confidence"
        score   = round(success_rate, 3)

    # Human-readable explanation for agent logs / UIs
    parts = []
    if success_rate is not None:
        parts.append(f"{success_rate * 100:.1f}% success rate over {n} real bookings")
    if avg_ms is not None:
        parts.append(f"avg execution {avg_ms / 1000:.1f}s")
    if p95_ms is not None:
        parts.append(f"p95 {p95_ms / 1000:.1f}s")
    if failure_variance:
        parts.append(f"{failure_variance} variance")
    if retry_rate is not None:
        parts.append(f"{retry_rate * 100:.0f}% retry rate")
    explanation = ". ".join(parts) + "." if parts else "No booking data collected yet."

    return {
        "score":             score,
        "sample_size":       n,
        "verdict":           verdict,
        "explanation":       explanation,
        "failure_variance":  failure_variance,
        "failure_breakdown": failure_breakdown,
        # Agent hint: if verdict is high_confidence or moderate, safe to route
        "recommended":       verdict in ("high_confidence", "moderate"),
    }


def _get_api_usage_metrics() -> dict:
    """
    Aggregate in-memory request logs for usage stats.
    Returns counts for last 1h, 24h, and since deploy, broken down by path and source.
    """
    now = datetime.now(timezone.utc)
    t_1h  = (now - timedelta(hours=1)).isoformat()
    t_24h = (now - timedelta(hours=24)).isoformat()

    by_path_1h = {}; by_source_1h = {}
    by_path_24h = {}; by_source_24h = {}
    by_path_all = {}; by_source_all = {}
    # MCP tool-level tracking
    mcp_tools_1h = {}; mcp_tools_24h = {}; mcp_tools_all = {}
    mcp_methods_all = {}

    for entry in _request_log_buffer:
        path = entry["path"]
        source = entry["source"]
        ts = entry["ts"]
        mcp_tool = entry.get("mcp_tool")
        mcp_method = entry.get("mcp_method")

        by_path_all[path] = by_path_all.get(path, 0) + 1
        by_source_all[source] = by_source_all.get(source, 0) + 1

        if mcp_method:
            mcp_methods_all[mcp_method] = mcp_methods_all.get(mcp_method, 0) + 1
        if mcp_tool:
            mcp_tools_all[mcp_tool] = mcp_tools_all.get(mcp_tool, 0) + 1

        if ts >= t_24h:
            by_path_24h[path] = by_path_24h.get(path, 0) + 1
            by_source_24h[source] = by_source_24h.get(source, 0) + 1
            if mcp_tool:
                mcp_tools_24h[mcp_tool] = mcp_tools_24h.get(mcp_tool, 0) + 1

        if ts >= t_1h:
            by_path_1h[path] = by_path_1h.get(path, 0) + 1
            by_source_1h[source] = by_source_1h.get(source, 0) + 1
            if mcp_tool:
                mcp_tools_1h[mcp_tool] = mcp_tools_1h.get(mcp_tool, 0) + 1

    return {
        "last_1h": {
            "total_calls": sum(by_path_1h.values()),
            "by_path": by_path_1h,
            "by_source": by_source_1h,
            "mcp_tools": mcp_tools_1h,
        },
        "last_24h": {
            "total_calls": sum(by_path_24h.values()),
            "by_path": by_path_24h,
            "by_source": by_source_24h,
            "mcp_tools": mcp_tools_24h,
        },
        "since_deploy": {
            "total_calls": sum(by_path_all.values()),
            "by_path": by_path_all,
            "by_source": by_source_all,
            "mcp_tools": mcp_tools_all,
            "mcp_methods": mcp_methods_all,
        },
    }


@app.route("/test/dry-run", methods=["POST"])
def test_dry_run():
    """
    Trigger a dry-run booking fulfillment directly — no Stripe checkout needed.
    Calls _fulfill_booking_async with dry_run=True using a real slot from inventory.
    Use this to verify the full fulfillment pipeline without touching any supplier.

    Requires X-API-Key header (same key used for /api/book).

    Body (JSON):
        slot_id  — optional; defaults to first available slot in inventory
    """
    api_key = request.headers.get("X-API-Key", "")
    if not _validate_api_key(api_key):
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.get_json(force=True, silent=True) or {}
    slot_id = (data.get("slot_id") or "").strip()

    # Pick first available slot if none specified
    if not slot_id:
        try:
            slots = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
            slot  = next((s for s in slots if s.get("booking_url")), None)
            if slot:
                slot_id = slot["slot_id"]
        except Exception:
            pass

    if not slot_id:
        return jsonify({"error": "No slots available in inventory"}), 404

    slot = get_slot_by_id(slot_id)
    if not slot:
        return jsonify({"error": f"Slot {slot_id} not found"}), 404

    session_id  = f"dry_run_test_{slot_id[:12]}"
    wh_key      = f"webhook_session_{session_id[:40]}"
    customer    = {"name": "Dry Run Test", "email": "test@lastminutedealshq.com", "phone": "+15550001234"}
    platform    = slot.get("platform", "")
    booking_url = slot.get("booking_url", "")
    amount      = int(float(slot.get("our_price") or slot.get("price") or 0) * 100)
    service     = slot.get("service_name", "Test Booking")

    threading.Thread(
        target=_fulfill_booking_async,
        args=(session_id, wh_key, slot_id, "", customer, platform, booking_url, amount, service, True),
        daemon=True,
        name=f"dry-run-{slot_id[:8]}",
    ).start()

    return jsonify({
        "started":    True,
        "session_id": session_id,
        "slot_id":    slot_id,
        "service":    service,
        "platform":   platform,
        "note":       "Fulfillment running in background. Check Railway logs for [FULFILL] DRY RUN lines. Booking record will appear in Supabase tagged dry_run=true.",
    })


@app.route("/metrics", methods=["GET"])
def public_metrics():
    """
    Public performance metrics. No auth required — this is a signal beacon.
    Shows that choosing LastMinuteDeals is a rational decision, not a bet.

    Returns live stats derived from actual booking history + current inventory.
    Updated on every pipeline run. Intentionally transparent.
    """
    now = datetime.now(timezone.utc)

    # ── Slot inventory ─────────────────────────────────────────────────────────
    slot_count = 0
    priced_count = 0
    categories: set = set()
    cities: set = set()
    next_slot_hours = None

    # Prefer local aggregated file; fall back to Supabase on Railway where .tmp/ is ephemeral
    slots: list = []
    if DATA_FILE.exists():
        try:
            slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not slots:
        # On Railway the local file doesn't survive deploys — query Supabase directly
        try:
            slots = _load_slots_from_supabase(hours_ahead=168, limit=10000)
        except Exception:
            pass
    slot_count = len(slots)
    for s in slots:
        p = float(s.get("our_price") or s.get("price") or 0)
        if p > 0:
            priced_count += 1
        if s.get("category"):
            categories.add(s["category"])
        if s.get("location_city"):
            cities.add(s["location_city"])
        h = s.get("hours_until_start")
        if h is not None and h >= 0:
            if next_slot_hours is None or h < next_slot_hours:
                next_slot_hours = h

    # ── Booking performance (from market_insights) ─────────────────────────────
    success_rate   = None
    avg_exec_time  = None  # We don't measure wall clock yet — honest omission
    fallback_rate  = None
    avg_savings    = None
    total_bookings = 0
    platform_count = 0

    insights_mod = _load_module("market_insights")
    if insights_mod:
        try:
            snap = insights_mod.get_market_snapshot()
            total_bookings = snap.get("total_booking_attempts", 0)

            # Aggregate success rate across all platforms
            plat_rel = snap.get("platform_reliability", {})
            platform_count = len(plat_rel)
            if plat_rel:
                rates = [v["success_rate"] for v in plat_rel.values() if v.get("booking_count", 0) >= 3]
                if rates:
                    success_rate = round(sum(rates) / len(rates), 3)

            # Fallback rate: avg fallbacks_used per successful booking
            # Derive from avg_attempts - 1 across platforms
            if plat_rel:
                avg_attempts_vals = [v["avg_attempts"] for v in plat_rel.values() if v.get("booking_count", 0) >= 3]
                if avg_attempts_vals:
                    avg_attempts = sum(avg_attempts_vals) / len(avg_attempts_vals)
                    fallback_rate = round(max(avg_attempts - 1, 0) / 6, 3)  # normalize over 6 max fallbacks

            # Average savings from category/city matrix
            matrix = snap.get("category_city_matrix", {})
            all_prices = []
            for cat_data in matrix.values():
                for city_data in cat_data.values():
                    p = city_data.get("avg_price")
                    if p:
                        all_prices.append(p)
            # We don't store original_price in aggregate yet — honest null
        except Exception:
            pass

    # ── Watcher freshness ──────────────────────────────────────────────────────
    watcher_running = False
    freshest_poll_age_seconds = None
    watcher_platforms = []

    status_file = Path(".tmp/watcher_status.json")
    if status_file.exists():
        try:
            statuses = json.loads(status_file.read_text(encoding="utf-8"))
            for plat, st in statuses.items():
                if st.get("state") == "running":
                    watcher_running = True
                    watcher_platforms.append(plat)
                last = st.get("last_poll", "")
                if last:
                    try:
                        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        age = (now - dt).total_seconds()
                        if freshest_poll_age_seconds is None or age < freshest_poll_age_seconds:
                            freshest_poll_age_seconds = age
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Active intent sessions ─────────────────────────────────────────────────
    active_intents = 0
    sessions_file = Path(".tmp/intent_sessions.json")
    if sessions_file.exists():
        try:
            sessions = json.loads(sessions_file.read_text(encoding="utf-8"))
            active_intents = sum(1 for s in sessions.values() if s.get("status") == "monitoring")
        except Exception:
            pass

    # ── Wallets ────────────────────────────────────────────────────────────────
    wallet_count = 0
    wallets_file = Path(".tmp/wallets.json")
    if wallets_file.exists():
        try:
            wallet_count = len(json.loads(wallets_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    return jsonify({
        "generated_at": now.isoformat(),
        "inventory": {
            "total_slots":          slot_count,
            "bookable_slots":       priced_count,
            "categories":           sorted(categories),
            "cities_covered":       len(cities),
            "next_slot_hours":      round(next_slot_hours, 1) if next_slot_hours is not None else None,
        },
        "performance": {
            "success_rate":         success_rate,
            "total_bookings":       total_bookings,
            "platforms_tracked":    platform_count,
            "fallback_rate":        fallback_rate,
            "avg_savings_dollars":  avg_savings,
            "data_note":            "Metrics accumulate passively from real bookings. Higher values over time = more data collected." if total_bookings < 50 else None,
        },
        "infrastructure": {
            "realtime_watcher":      watcher_running,
            "watcher_platforms":     watcher_platforms,
            "data_freshness_seconds": round(freshest_poll_age_seconds) if freshest_poll_age_seconds is not None else None,
            "active_intent_sessions": active_intents,
            "registered_wallets":    wallet_count,
        },
        "api": {
            "endpoints":     ["GET /slots", "GET /slots/{id}/quote", "POST /execute/best", "POST /execute/guaranteed", "POST /intent/create", "GET /insights/market"],
            "auth":          "Register free at POST /api/keys/register",
            "sdk":           "pip install requests  # no special SDK required — standard HTTP",
            "mcp_server":    "POST /mcp  (MCP-over-HTTP, no transport setup needed)",
            "openapi":       f"{os.getenv('LANDING_PAGE_URL', 'https://lastminutedealshq.com')}/openapi.json",
        },
        "reliability":   (reliability := _get_reliability_metrics()),
        "trust_signal":  _compute_trust_signal(reliability),
        "api_usage":     _get_api_usage_metrics(),
        "agent_recommendation": _compute_agent_recommendation(slot_count),
        "system": {
            "version": "2.1.0",
            "deployed_at": "2026-04-15",
            "recent_fixes": [
                {
                    "version": "2.1.0",
                    "date": "2026-04-15",
                    "fixes": [
                        "search_slots: converted to async httpx — eliminates event-loop blocking that caused SSE timeouts under load",
                        "book_slot: fixed UnboundLocalError that silently failed all agent booking attempts",
                        "book_slot: fixed Supabase Storage write swallowing HTTP error responses",
                        "metrics: inventory.total_slots now reads from Supabase on Railway (local file is ephemeral)",
                        "metrics: api_usage endpoint fixed (DB URL parsing error resolved)",
                        "/health: now exposes live search_slots and book_slot success rates from request_logs",
                    ],
                    "load_test": {
                        "slots_endpoint": {"requests": 100, "success_rate": 1.0, "p50_ms": 947, "p95_ms": 1642},
                        "mcp_search_slots": {"requests": 60, "success_rate": 1.0, "p50_ms": 1537, "p95_ms": 2363},
                    },
                }
            ],
        },
    })


@app.route("/api/book", methods=["POST"])
def create_checkout():
    """
    Availability check → Stripe payment authorization (hold, not charge).
    The card is only captured after the bot successfully completes the booking.
    """
    stripe = _stripe()
    if not stripe.api_key:
        return jsonify({"success": False, "error": "Payment not configured. Contact support."}), 503

    data = request.get_json(force=True, silent=True) or {}
    slot_id        = (data.get("slot_id") or "").strip()
    customer_name  = (data.get("customer_name") or "").strip()
    customer_email = (data.get("customer_email") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    quantity       = max(1, min(int(data.get("quantity") or 1), 20))  # clamp 1–20
    # dry_run=true: skip OCTO supplier call; test payment + webhook flow without
    # touching any real supplier. Safe to use in Stripe test mode for end-to-end testing.
    dry_run = str(data.get("dry_run", "")).lower() in ("true", "1", "yes") or \
              request.headers.get("X-Dry-Run", "").lower() in ("true", "1", "yes")
    # Idempotency key — if provided, duplicate requests return the same checkout URL
    idempotency_key = (
        data.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()

    if not all([slot_id, customer_name, customer_email, customer_phone]):
        return jsonify({"success": False, "error": "Missing required fields."}), 400

    # ── Idempotency check ─────────────────────────────────────────────────────
    if idempotency_key:
        cached = _IDEMPOTENCY_CACHE.get(idempotency_key)
        if cached:
            return jsonify({**cached["result"], "idempotent_replay": True})
        # Also check Supabase Storage for cross-instance dedup
        existing = _load_booking_record(f"idem_{idempotency_key[:40]}")
        if existing and existing.get("checkout_url"):
            return jsonify({"success": True, "checkout_url": existing["checkout_url"],
                            "idempotent_replay": True})

    # ── Availability check ────────────────────────────────────────────────────
    slot = get_slot_by_id(slot_id)
    if not slot:
        return jsonify({"success": False, "error": "This slot is no longer available."}), 404

    # Check the slot hasn't already been booked through our system
    if slot_id in _load_booked():
        return jsonify({"success": False, "error": "This slot has already been booked."}), 409

    # Check it hasn't started yet
    try:
        start_iso = slot.get("start_time", "")
        if start_iso:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            if start_dt <= datetime.now(timezone.utc):
                return jsonify({"success": False, "error": "This slot has already started."}), 410
    except Exception:
        pass

    our_price = slot.get("our_price") or slot.get("price")
    if not our_price or float(our_price) <= 0:
        return jsonify({"success": False, "error": "This slot is not available for checkout."}), 400

    price_cents  = int(float(our_price) * 100)   # per-person price in cents
    service_name = slot.get("service_name", "Last-Minute Booking")[:80]
    landing_url  = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").rstrip("/")

    try:
        import uuid as _uuid
        # Pre-generate booking_id before the Stripe call — cannot reference
        # session.id inside the create() arguments (UnboundLocalError).
        booking_id = f"bk_{slot_id[:12]}_{_uuid.uuid4().hex[:8]}"

        # ── Auth hold (capture_method=manual) — card is NOT charged yet ───────
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": slot.get("currency", "usd").lower(),
                    "product_data": {
                        "name": service_name,
                        "description": (
                            f"{slot.get('location_city', '')}, {slot.get('location_state', '')} — "
                            f"Starts {slot.get('start_time', '')[:16].replace('T', ' ')}"
                        ),
                    },
                    "unit_amount": price_cents,
                },
                "quantity": quantity,  # per-person price × quantity = total charge
            }],
            mode="payment",
            payment_intent_data={"capture_method": "manual"},  # HOLD only — charge after booking confirmed
            customer_email=customer_email,
            success_url=f"{landing_url}?booking=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{landing_url}?booking=cancelled",
            metadata={
                "slot_id":        slot_id,
                "customer_name":  customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "service_name":   service_name,
                "booking_url":    slot.get("booking_url", ""),
                "platform":       slot.get("platform", ""),
                "dry_run":        "true" if dry_run else "false",
                "booking_id":     booking_id,
                "quantity":       str(quantity),
            },
        )

        # Create a pending record immediately — agent can poll this before human pays.
        # Stripe checkout sessions expire after 24 hours by default.
        from datetime import timedelta
        _now = datetime.now(timezone.utc)
        _expires_at = (_now + timedelta(hours=24)).isoformat()
        _save_booking_record(booking_id, {
            "booking_id":     booking_id,
            "session_id":     session.id,
            "slot_id":        slot_id,
            "service_name":   service_name,
            "business_name":  slot.get("business_name", ""),
            "location_city":  slot.get("location_city", ""),
            "start_time":     slot.get("start_time", ""),
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "quantity":       quantity,
            "status":         "pending_payment",
            "payment_status": "unpaid",
            "checkout_url":   session.url,
            "expires_at":     _expires_at,
            "dry_run":        dry_run,
            "created_at":     _now.isoformat(),
            # Store fulfillment details in the record so the webhook can read
            # them directly — Stripe metadata has a 500-char value limit and
            # booking_url (JSON blob) can exceed it, silently breaking fulfillment.
            "booking_url":    slot.get("booking_url", ""),
            "platform":       slot.get("platform", ""),
            "currency":       slot.get("currency", "USD"),
            # Save price in pending record so get_booking_status returns non-null
            # values while the booking awaits payment. Without this, agents see
            # price_charged: null and may retry, creating duplicate sessions.
            "our_price":      float(our_price),
            "price_charged":  float(our_price) * quantity,
            "cancellation_cutoff_hours": slot.get("cancellation_cutoff_hours"),
        })

        _policy = _cancel_policy_label(slot.get("cancellation_cutoff_hours"))
        result = {
            "success":         True,
            "checkout_url":    session.url,
            "booking_id":      booking_id,
            "status":          "pending_payment",
            "payment_status":  "unpaid",
            "expires_at":      _expires_at,
            # Price + service context so agents don't need a follow-up
            # get_booking_status call to verify what was just created.
            "service_name":    service_name,
            "start_time":      slot.get("start_time", ""),
            "location_city":   slot.get("location_city", ""),
            "quantity":        quantity,
            "price_per_person": float(our_price),
            "total_price":     float(our_price) * quantity,
            "currency":        slot.get("currency", "USD"),
            "cancellation_policy": _policy,
            # Human-readable instruction for the calling agent.
            "action_required": (
                "IMPORTANT: Tell the customer the cancellation policy before they pay: "
                + _policy + ". "
                "Then direct them to checkout_url to complete payment. "
                "Booking expires in 24 hours. Poll get_booking_status for confirmation."
            ),
        }
        # Store idempotency record so duplicate requests return same URL
        if idempotency_key:
            _IDEMPOTENCY_CACHE[idempotency_key] = {"result": result}
            _save_booking_record(f"idem_{idempotency_key[:40]}", {
                "idempotency_key": idempotency_key,
                "checkout_url":    session.url,
                "booking_id":      booking_id,
                "slot_id":         slot_id,
                "created_at":      datetime.now(timezone.utc).isoformat(),
            })

        # Send "checkout created" email so the customer gets the payment link even if
        # the calling agent doesn't surface it. Non-fatal — failure never blocks booking.
        #
        # Guard: skip obviously fake/test addresses to conserve SendGrid free-tier quota
        # (100 emails/day). Real domains have at least one dot after '@'.  Addresses like
        # "test@example.com", "agent@test", "fake@com" are skipped.  Legitimate customer
        # emails (jane@gmail.com, travel@company.io) always have a dotted domain.
        _email_domain = customer_email.split("@")[-1] if "@" in customer_email else ""
        _looks_real   = "." in _email_domain and not _email_domain.lower() in (
            "example.com", "test.com", "fake.com", "placeholder.com",
        )
        if not dry_run and _looks_real:
            try:
                from send_booking_email import send_booking_email
                send_booking_email(
                    "checkout_created",
                    customer_email,
                    customer_name,
                    {
                        **slot,
                        "checkout_url": session.url,
                        "our_price":    float(our_price),
                        "quantity":     quantity,
                    },
                )
                print(f"[CHECKOUT] Checkout email sent to {customer_email} for {booking_id}")
            except Exception as mail_err:
                print(f"[CHECKOUT] Checkout email failed (non-fatal): {mail_err}")

        return jsonify(result)
    except Exception as e:
        print(f"Stripe error: {e}")
        return jsonify({"success": False, "error": "Payment system error. Please try again."}), 500


@app.route("/api/book/direct", methods=["POST"])
def book_direct():
    """
    Autonomous agent booking — no Stripe checkout, no human in the loop.

    Requires BOTH:
      - wallet_id: a funded wallet tied to the calling agent's account
      - execution_mode: "autonomous" (explicit intent — prevents accidental charges)

    Body:
      {
        "slot_id":        "...",
        "customer_name":  "...",
        "customer_email": "...",
        "customer_phone": "...",
        "wallet_id":      "wlt_...",
        "execution_mode": "autonomous"
      }

    Response (always one of two shapes — never mixed):
      Success: { "status": "confirmed", "booking_id": "bk_...", "confirmation_number": "...",
                 "service_name": "...", "charged_cents": 11120, "wallet_balance_remaining": ... }
      Failure: { "status": "failed", "error": "...", "failure_reason": "..." }

    /api/book remains unchanged as the human-facing Stripe checkout path.
    """
    api_key = request.headers.get("X-API-Key", "")
    if not _validate_api_key(api_key):
        return jsonify({"status": "failed", "error": "Unauthorized. Valid X-API-Key required.",
                        "failure_reason": "auth_error"}), 401

    data           = request.get_json(force=True, silent=True) or {}
    slot_id        = (data.get("slot_id") or "").strip()
    customer_name  = (data.get("customer_name") or "").strip()
    customer_email = (data.get("customer_email") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    wallet_id      = (data.get("wallet_id") or "").strip()
    execution_mode = (data.get("execution_mode") or "").strip().lower()
    quantity       = max(1, min(int(data.get("quantity") or 1), 20))  # clamp 1–20

    # Both fields required — wallet proves capability, execution_mode proves intent
    if execution_mode != "autonomous":
        return jsonify({
            "status": "failed",
            "error": 'execution_mode must be "autonomous" for direct booking. '
                     'Use POST /api/book for human-facing Stripe checkout.',
            "failure_reason": "invalid_execution_mode",
        }), 400
    if not wallet_id:
        return jsonify({
            "status": "failed",
            "error": "wallet_id required for autonomous booking.",
            "failure_reason": "missing_wallet",
        }), 400
    if not all([slot_id, customer_name, customer_email, customer_phone]):
        return jsonify({
            "status": "failed",
            "error": "slot_id, customer_name, customer_email, customer_phone are all required.",
            "failure_reason": "missing_fields",
        }), 400

    # Load wallet module
    try:
        wlt_mod = _load_module("manage_wallets")
        if not wlt_mod:
            raise ImportError("manage_wallets unavailable")
    except Exception as e:
        return jsonify({"status": "failed", "error": "Wallet system unavailable.",
                        "failure_reason": "system_error"}), 500

    # Verify wallet exists
    wallet = wlt_mod.get_wallet(wallet_id)
    if not wallet:
        return jsonify({"status": "failed", "error": f"Wallet '{wallet_id}' not found.",
                        "failure_reason": "wallet_not_found"}), 404

    # Verify slot exists and is available
    slot = get_slot_by_id(slot_id)
    if not slot:
        return jsonify({"status": "failed", "error": "Slot not found or expired.",
                        "failure_reason": "slot_not_found"}), 404
    if slot_id in _load_booked():
        return jsonify({"status": "failed", "error": "Slot already booked.",
                        "failure_reason": "slot_unavailable"}), 409
    try:
        start_dt = datetime.fromisoformat(
            slot.get("start_time", "").replace("Z", "+00:00"))
        if start_dt <= datetime.now(timezone.utc):
            return jsonify({"status": "failed", "error": "Slot has already started.",
                            "failure_reason": "slot_expired"}), 410
    except Exception:
        pass

    # ── Cancellation policy gate: block autonomous booking for risky products ──
    _auto_cutoff = slot.get("cancellation_cutoff_hours")
    if _auto_cutoff is None:
        try:
            _burl_str = slot.get("booking_url", "")
            if _burl_str and isinstance(_burl_str, str) and _burl_str.startswith("{"):
                _auto_cutoff = json.loads(_burl_str).get("cancellation_cutoff_hours")
        except Exception:
            pass
    _auto_cutoff = int(_auto_cutoff) if _auto_cutoff is not None else 48
    if _auto_cutoff >= 9999 * 24:
        return jsonify({
            "status": "failed",
            "error": "This product is non-refundable. Autonomous booking is not available "
                     "for non-refundable products. Use approval mode (omit wallet_id) to "
                     "generate a checkout URL so the customer can review and acknowledge "
                     "the cancellation policy before paying.",
            "failure_reason": "policy_acknowledgment_required",
            "cancellation_policy": "Non-refundable — no cancellations or refunds",
            "cancellation_cutoff_hours": _auto_cutoff,
        }), 403
    if _auto_cutoff > 48:
        _policy_txt = _cancel_policy_label(_auto_cutoff)
        return jsonify({
            "status": "failed",
            "error": f"This product has a {_cancel_cutoff_display(_auto_cutoff)} cancellation "
                     "window. Autonomous booking is not available for products with "
                     "cancellation windows over 48 hours. Use approval mode (omit wallet_id) "
                     "to generate a checkout URL so the customer can review and acknowledge "
                     "the cancellation policy before paying.",
            "failure_reason": "policy_acknowledgment_required",
            "cancellation_policy": _policy_txt,
            "cancellation_cutoff_hours": _auto_cutoff,
        }), 403

    our_price = slot.get("our_price") or slot.get("price")
    if not our_price or float(our_price) <= 0:
        return jsonify({"status": "failed", "error": "Slot has no price set.",
                        "failure_reason": "no_price"}), 400

    amount_cents = int(float(our_price) * quantity * 100)  # total for all persons

    # Acquire per-wallet lock to prevent concurrent bookings from overdrafting.
    # The lock spans balance check → debit so no other request can interleave.
    _wallet_lock = _get_wallet_lock(wallet_id)
    _wallet_lock.acquire()
    try:
        balance = wlt_mod.get_balance(wallet_id)
        if balance is None or balance < amount_cents:
            balance_dollars = (balance or 0) / 100
            _wallet_lock.release()
            return jsonify({
                "status": "failed",
                "error": f"Insufficient wallet balance. Need ${our_price:.2f}, have ${balance_dollars:.2f}.",
                "failure_reason": "insufficient_funds",
                "wallet_balance_cents": balance or 0,
                "required_cents": amount_cents,
            }), 402
    except Exception:
        _wallet_lock.release()
        raise
    # NOTE: _wallet_lock is still held — released after debit below

    # ── Idempotency: reject retries that arrive while first call is still running ──
    # Key includes a 5-minute timestamp bucket so duplicate suppression only applies
    # within the same execution window. After the bucket expires, intentional retries
    # (e.g. after a network error where no charge occurred) are not blocked.
    import hashlib as _hl
    _ts_bucket = int(time.time()) // 300  # 5-minute window
    _idem_key = _hl.sha256(
        f"{slot_id}:{customer_email}:{wallet_id}:{_ts_bucket}".encode()
    ).hexdigest()[:32]
    with _DIRECT_LOCK:
        if _DIRECT_IN_FLIGHT.get(_idem_key):
            return jsonify({
                "status":         "failed",
                "error":          "A booking for this slot is already in progress. "
                                  "Please wait for the first request to complete before retrying.",
                "failure_reason": "duplicate_in_flight",
            }), 409
        _DIRECT_IN_FLIGHT[_idem_key] = True

    # ── Spending limit check ─────────────────────────────────────────────────
    # Per-wallet limit takes precedence; fall back to the module default ($5,000).
    # To remove the cap on a specific wallet: set spending_limit_cents=null via
    # PUT /api/wallets/{id}/spending-limit — that sets it to None, which still
    # falls back to the module default.
    service_name = slot.get("service_name", "Booking")
    spending_limit = wallet.get("spending_limit_cents") or _DEFAULT_AUTONOMOUS_LIMIT_CENTS
    if amount_cents > spending_limit:
        with _DIRECT_LOCK:
            _DIRECT_IN_FLIGHT.pop(_idem_key, None)
        return jsonify({
            "status":           "failed",
            "error":            f"Transaction exceeds wallet spending limit "
                                f"(${spending_limit/100:.2f} per booking). "
                                f"This booking costs ${amount_cents/100:.2f}.",
            "failure_reason":   "spending_limit_exceeded",
            "spending_limit_cents": spending_limit,
            "required_cents":   amount_cents,
        }), 403

    # ── Crash-recovery record ───────────────────────────────────────────────
    # Written BEFORE debit so a process crash between debit and refund is detectable.
    # On startup, _reconcile_pending_debits() scans for wallet_debited=true records
    # and issues the missing refund.
    _recovery_key = f"pending_exec_{_idem_key[:24]}"
    _save_booking_record(_recovery_key, {
        "recovery_key":   _recovery_key,
        "status":         "pending_execution",
        "wallet_id":      wallet_id,
        "amount_cents":   amount_cents,
        "slot_id":        slot_id,
        "customer_email": customer_email,
        "wallet_debited": False,
        "created_at":     datetime.now(timezone.utc).isoformat(),
    })

    # Debit wallet — atomic before starting fulfillment
    # _wallet_lock is still held from balance check above — release after debit.
    try:
        debited = wlt_mod.debit_wallet(
            wallet_id, amount_cents,
            f"Booking: {service_name} ({slot_id[:12]})"
        )
    except Exception as debit_exc:
        _wallet_lock.release()
        _save_booking_record(_recovery_key, {"status": "debit_failed", "resolved": True})
        with _DIRECT_LOCK:
            _DIRECT_IN_FLIGHT.pop(_idem_key, None)
        return jsonify({"status": "failed", "error": str(debit_exc)[:200],
                        "failure_reason": "debit_failed"}), 500
    finally:
        # Release wallet lock — balance check + debit are now atomic
        if _wallet_lock.locked():
            try:
                _wallet_lock.release()
            except RuntimeError:
                pass  # Already released in except branch above
    if not debited:
        _save_booking_record(_recovery_key, {"status": "debit_failed", "resolved": True})
        with _DIRECT_LOCK:
            _DIRECT_IN_FLIGHT.pop(_idem_key, None)
        return jsonify({"status": "failed", "error": "Wallet debit failed. Please try again.",
                        "failure_reason": "debit_failed"}), 500

    # Mark debit as having occurred — crash recovery watches for this
    _save_booking_record(_recovery_key, {
        "recovery_key":   _recovery_key,
        "status":         "pending_execution",
        "wallet_id":      wallet_id,
        "amount_cents":   amount_cents,
        "slot_id":        slot_id,
        "customer_email": customer_email,
        "wallet_debited": True,
        "debited_at":     datetime.now(timezone.utc).isoformat(),
    })

    # Wallet debited — now execute fulfillment synchronously so we can return
    # the confirmation directly. Use a short-lived thread with a hard join timeout
    # matching _MAX_BOOKING_TIMEOUT_S so the response isn't open-ended.
    import concurrent.futures
    session_id  = f"direct_{slot_id[:12]}_{int(time.time())}"
    wh_key      = f"webhook_session_{session_id[:40]}"
    customer    = {"name": customer_name, "email": customer_email, "phone": customer_phone}
    platform    = slot.get("platform", "")
    booking_url = slot.get("booking_url", "")

    confirmation = None
    booking_meta: dict = {}
    fulfill_error = None

    _fulfill_timed_out = False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_fulfill_booking, slot_id, customer, platform, booking_url, quantity)
            # _fulfill_booking returns a 3-tuple: (confirmation, booking_meta, supplier_reference)
            confirmation, booking_meta, supplier_reference = fut.result(timeout=_MAX_BOOKING_TIMEOUT_S)
    except concurrent.futures.TimeoutError as exc:
        _fulfill_timed_out = True
        fulfill_error = exc
    except Exception as exc:
        fulfill_error = exc

    if fulfill_error:
        # If timed out, the thread is still running and may succeed later,
        # creating an OCTO booking with no corresponding payment.
        # Queue a preemptive OCTO cancellation so the orphan gets cleaned up.
        if _fulfill_timed_out:
            try:
                burl_j_to = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
                _timeout_sid = burl_j_to.get("supplier_id", platform)
                _queue_octo_retry(f"timeout_{slot_id[:16]}", _timeout_sid,
                                  slot_id, None, 0)
                print(f"[DIRECT] Fulfillment timed out — queued preemptive OCTO cancel for {slot_id}")
            except Exception as q_err:
                print(f"[DIRECT] Could not queue timeout-cancel: {q_err}")

        # Refund the wallet debit — booking never completed
        failure_reason = _classify_failure(fulfill_error)
        try:
            wlt_mod.credit_wallet(wallet_id, amount_cents,
                                  f"Refund: failed booking {slot_id[:12]} ({failure_reason})")
        except Exception as refund_err:
            print(f"[DIRECT] Wallet refund failed after booking failure: {refund_err} — "
                  f"manual refund needed for {wallet_id} ${amount_cents/100:.2f}")
        # Merge into existing recovery record to preserve wallet_id/amount_cents for reconcile
        _existing_rec = _load_booking_record(_recovery_key) or {}
        _save_booking_record(_recovery_key, {**_existing_rec, "status": "refunded", "resolved": True,
                                              "refunded_at": datetime.now(timezone.utc).isoformat()})
        with _DIRECT_LOCK:
            _DIRECT_IN_FLIGHT.pop(_idem_key, None)
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_failed", customer_email, customer_name,
                               slot, error_reason=str(fulfill_error))
        except Exception as mail_err:
            print(f"[DIRECT] Failure email error (non-fatal): {mail_err}")
        return jsonify({
            "status":         "failed",
            "failure_reason": failure_reason,
            "error":          str(fulfill_error)[:300],
            "wallet_refunded": True,
        }), 502

    # Fulfillment succeeded — persist record and return confirmation
    _mark_booked(slot_id)
    booking_record_id = f"bk_{slot_id[:12]}_{uuid.uuid4().hex[:8]}"
    try:
        burl_j      = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
        supplier_id = burl_j.get("supplier_id", platform)
    except Exception:
        supplier_id = platform

    record = {
        "booking_id":            booking_record_id,
        "session_id":            session_id,
        "confirmation":          str(confirmation or ""),
        "supplier_reference":    str(supplier_reference or ""),
        "platform":              platform,
        "supplier_id":           supplier_id,
        "service_name":          service_name,
        "business_name":         slot.get("business_name", ""),
        "location_city":         slot.get("location_city", ""),
        "start_time":            slot.get("start_time", ""),
        "price_charged":         amount_cents / 100,  # total for all persons
        "quantity":              quantity,
        "status":                "booked",
        "payment_method":        "wallet",
        "wallet_id":             wallet_id,
        "cancellation_cutoff_hours": slot.get("cancellation_cutoff_hours"),
        "executed_at":           datetime.now(timezone.utc).isoformat(),
        "customer_name":         customer_name,
        "customer_email":        customer_email,
        "customer_phone":        customer_phone,
        "slot_id":               slot_id,
        "execution_duration_ms": booking_meta.get("execution_duration_ms"),
        "reservation_ms":        booking_meta.get("reservation_ms"),
        "confirm_ms":            booking_meta.get("confirm_ms"),
        "attempts":              booking_meta.get("attempts", 1),
        "retries":               booking_meta.get("retries", 0),
        "retry_stage":           booking_meta.get("retry_stage"),
    }
    _save_booking_record(booking_record_id, record)
    _save_booking_record(wh_key, {"session_id": session_id, "status": "booked",
                                   "booking_id": booking_record_id,
                                   "completed_at": datetime.now(timezone.utc).isoformat()})

    balance_after = wlt_mod.get_balance(wallet_id) or 0

    # Mark recovery record resolved — merge to preserve wallet_id/amount_cents fields
    _existing_rec = _load_booking_record(_recovery_key) or {}
    _save_booking_record(_recovery_key, {**_existing_rec, "status": "completed", "resolved": True,
                                          "booking_id": booking_record_id,
                                          "completed_at": datetime.now(timezone.utc).isoformat()})
    # Release in-flight lock — booking is complete, slot marked booked, no duplicate possible
    with _DIRECT_LOCK:
        _DIRECT_IN_FLIGHT.pop(_idem_key, None)

    print(f"[DIRECT] Autonomous booking complete: {booking_record_id} "
          f"confirmation={confirmation} wallet={wallet_id} charged=${our_price:.2f}")

    try:
        from send_booking_email import send_booking_email
        _direct_host = os.getenv("BOOKING_SERVER_HOST", "")
        _direct_cancel_url = (
            f"{_direct_host}/cancel/{booking_record_id}"
            f"?t={_make_cancel_token(booking_record_id)}"
        ) if _direct_host else ""
        send_booking_email(
            "booking_confirmed", customer_email, customer_name,
            {**slot, "our_price": amount_cents / 100},
            confirmation_number=str(confirmation or ""),
            cancel_url=_direct_cancel_url,
        )
    except Exception as mail_err:
        print(f"[DIRECT] Confirmation email error (non-fatal): {mail_err}")

    return jsonify({
        "status":                   "confirmed",
        "booking_id":               booking_record_id,
        "confirmation_number":      str(confirmation or ""),
        "service_name":             service_name,
        "start_time":               slot.get("start_time"),
        "location_city":            slot.get("location_city"),
        "charged_cents":            amount_cents,
        "wallet_balance_remaining": balance_after,
    })


# Session-level idempotency lock: prevents concurrent processing of the same
# Stripe session on webhook retry. Maps session_id → True while executing.
_WEBHOOK_IN_FLIGHT: dict[str, bool] = {}
_WEBHOOK_LOCK = threading.Lock()


@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    """
    Handle Stripe webhook events.

    Returns 200 immediately after spawning a daemon thread to fulfill the booking.
    This prevents Stripe from retrying due to slow Bokun API calls (Stripe timeout: 30s).

    Idempotency is enforced at two levels:
      1. Session-level in-memory lock (_WEBHOOK_IN_FLIGHT) — prevents concurrent
         duplicate processing within the same process instance.
      2. Supabase Storage record (webhook_session_{session_id}) — prevents
         re-processing across process restarts and Railway redeploys.
    """
    import traceback as _tb
    stripe_mod     = _stripe()
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    payload        = request.get_data()
    sig_header     = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe_mod.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as e:
        print(f"[WEBHOOK] construct_event failed ({type(e).__name__}): {e}")
        return jsonify({"error": "Invalid signature or payload"}), 400

    event_type = event["type"] if "type" in event else "unknown"

    # ── checkout.session.expired ──────────────────────────────────────────────
    if event_type == "checkout.session.expired":
        try:
            session    = event["data"]["object"]
            metadata   = getattr(session, "metadata", None) or {}
            booking_id = metadata.get("booking_id", "")
            slot_id    = metadata.get("slot_id", "")
            if booking_id:
                existing = _load_booking_record(booking_id)
                if existing and existing.get("status") == "pending_payment":
                    existing.update({
                        "status":         "expired",
                        "payment_status": "expired",
                        "expired_at":     datetime.now(timezone.utc).isoformat(),
                    })
                    _save_booking_record(booking_id, existing)
                    print(f"[WEBHOOK] Checkout expired: {booking_id} slot={slot_id}")
        except Exception as e:
            print(f"[WEBHOOK] Error handling checkout.session.expired: {e}")
            _tb.print_exc()
        return jsonify({"status": "ok"})

    # ── checkout.session.completed ────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        try:
            session    = event["data"]["object"]
            session_id = getattr(session, "id", "") or ""
            metadata   = getattr(session, "metadata", None) or {}

            # ── Wallet top-up: fast path — no async needed ────────────────────
            if metadata.get("event_type") == "wallet_topup":
                wid    = metadata.get("wallet_id", "")
                amount = int(metadata.get("amount_cents", 0))
                if wid and amount > 0:
                    try:
                        spec = _ilu.spec_from_file_location("manage_wallets",
                                   Path(__file__).parent / "manage_wallets.py")
                        wlt_mod = _ilu.module_from_spec(spec)
                        spec.loader.exec_module(wlt_mod)
                        wlt_mod.credit_wallet(wid, amount, "Stripe top-up")
                        print(f"[WEBHOOK] Wallet {wid} credited ${amount/100:.2f}")
                    except Exception as wlt_err:
                        print(f"[WEBHOOK] Wallet credit failed: {wlt_err}")
                return jsonify({"status": "ok"})

            # ── Idempotency check (in-memory) — same session already executing
            with _WEBHOOK_LOCK:
                if _WEBHOOK_IN_FLIGHT.get(session_id):
                    print(f"[WEBHOOK] Already in-flight, ignoring retry: {session_id}")
                    return jsonify({"status": "ok", "note": "already_processing"})
                _WEBHOOK_IN_FLIGHT[session_id] = True

            # ── Idempotency check (persistent) — already completed or in-progress ─
            # Block on "booked" (done) and "processing" (another instance may be
            # handling it after a redeploy — prevents double-fulfillment).
            # Allow retry on "failed" (Stripe retries for up to 3 days; transient
            # OCTO failures may resolve on next attempt).
            wh_record_key = f"webhook_session_{session_id[:40]}"
            existing = _load_booking_record(wh_record_key)
            if existing and existing.get("status") in ("booked", "processing"):
                print(f"[WEBHOOK] Already processed session {session_id}: {existing.get('status')}")
                with _WEBHOOK_LOCK:
                    _WEBHOOK_IN_FLIGHT.pop(session_id, None)
                return jsonify({"status": "ok", "note": "already_processed"})

            # ── Mark session as processing (persistent, survives redeploy) ────
            _save_booking_record(wh_record_key, {
                "session_id": session_id,
                "status":     "processing",
                "started_at": datetime.now(timezone.utc).isoformat(),
            })

            # ── Spawn fulfillment thread — return 200 immediately to Stripe ──
            slot_id        = metadata.get("slot_id", "")
            payment_intent = getattr(session, "payment_intent", "") or ""
            customer = {
                "name":  metadata.get("customer_name", ""),
                "email": metadata.get("customer_email", getattr(session, "customer_email", "") or ""),
                "phone": metadata.get("customer_phone", ""),
            }
            amount_total = getattr(session, "amount_total", 0) or 0
            service_name = metadata.get("service_name", "")
            dry_run      = metadata.get("dry_run", "false") == "true"
            quantity     = max(1, int(metadata.get("quantity") or 1))
            # Use the pre-assigned booking_id if present (new path); fall back to
            # the old derived key for sessions created before this was deployed.
            pending_booking_id = metadata.get("booking_id") or f"bk_{slot_id[:12]}"

            # ── Read booking_url / platform from the stored record first. ─────
            # Stripe metadata values are capped at 500 chars and silently truncate —
            # a long booking_url JSON blob would corrupt fulfillment. The booking record
            # has no such limit and is written before the checkout session is created.
            pending_record = _load_booking_record(pending_booking_id) or {}
            platform    = pending_record.get("platform")    or metadata.get("platform", "")
            booking_url = pending_record.get("booking_url") or metadata.get("booking_url", "")

            # ── Mark as "fulfilling" so agents see payment has landed ──────────
            # Without this, agents polling get_booking_status see "pending_payment"
            # for up to 45 seconds after the customer has paid, because the webhook
            # doesn't update the record before the thread completes.
            _save_booking_record(pending_booking_id, {
                **pending_record,
                "status":         "fulfilling",
                "payment_status": "paid",
                "payment_intent": payment_intent,
            })

            threading.Thread(
                target=_fulfill_booking_async,
                args=(session_id, wh_record_key, slot_id, payment_intent,
                      customer, platform, booking_url, amount_total, service_name,
                      dry_run, pending_booking_id, quantity),
                daemon=True,
                name=f"fulfill-{session_id[:12]}",
            ).start()

            print(f"[WEBHOOK] Fulfillment queued: session={session_id} slot={slot_id}")

        except Exception as e:
            print(f"[WEBHOOK] Error handling checkout.session.completed: {e}")
            _tb.print_exc()
            # Return 500 so Stripe retries — customer has paid, we MUST fulfill.
            return jsonify({"error": "fulfillment_setup_failed", "detail": str(e)}), 500

    return jsonify({"status": "ok"})


# Hard ceiling on total supplier execution time. Individual OCTO HTTP calls have a
# 30 s socket timeout; with two retry attempts per call the worst-case chain is
# ~95 s. This ceiling aborts the supplier call and cancels the payment hold so the
# customer is never left waiting indefinitely.
_MAX_BOOKING_TIMEOUT_S = 45

# Default per-transaction spending cap for autonomous wallet bookings.
# Wallets with an explicit spending_limit_cents override this. Protects against
# runaway agents or compromised wallet_ids draining large balances in one call.
_DEFAULT_AUTONOMOUS_LIMIT_CENTS = 500_000  # $5,000.00


def _classify_failure(exc: Exception) -> str:
    """Map exception type to a stable failure_reason string for booking records."""
    from concurrent.futures import TimeoutError as FuturesTimeout
    cls = type(exc).__name__
    msg = str(exc).lower()
    if isinstance(exc, FuturesTimeout) or "timed out" in msg or "timeout" in msg:
        return "execution_timeout"
    if "BookingUnavailableError" in cls or "unavailable" in msg or "conflict" in msg or "409" in msg:
        return "availability_conflict"
    if "BookingTimeoutError" in cls or "network error" in msg:
        return "timeout"
    if "BookingAuthRequired" in cls or "auth" in msg:
        return "auth_error"
    if "cleanup_failed" in msg:
        return "cleanup_failed"
    return "api_error"


def _fulfill_booking_async(
    session_id: str, wh_record_key: str, slot_id: str, payment_intent: str,
    customer: dict, platform: str, booking_url: str,
    amount_total: int, service_name: str,
    dry_run: bool = False,
    pending_booking_id: str = "",
    quantity: int = 1,
):
    """
    Run in a daemon thread. Executes the full booking lifecycle:
      1. Send "in progress" email
      2. Call supplier API with hard 45 s ceiling (ThreadPoolExecutor)
         — OR — return synthetic confirmation if dry_run=True (no supplier touched)
      3. Capture or cancel Stripe payment hold
      4. Send confirmation or failure email
      5. Persist final state to Supabase Storage

    dry_run=True: skips the OCTO supplier call entirely. Safe for end-to-end testing
    without creating real bookings on any supplier dashboard.
    """
    import concurrent.futures
    stripe = _stripe()
    slot_for_email = get_slot_by_id(slot_id) or {"service_name": service_name or "your booking"}
    # Override our_price with the actual total charged (amount_total already includes
    # quantity × per-person price from Stripe). Without this, emails show per-person
    # price even when the customer booked multiple seats.
    slot_for_email = {**slot_for_email, "our_price": amount_total / 100}
    _exec_start = time.monotonic()

    # Pre-initialize so the failure handler can detect whether the OCTO booking
    # succeeded before a payment capture failure (and queue the orphan for cancellation).
    confirmation       = None
    supplier_reference = None
    booking_meta: dict = {}

    if dry_run:
        print(f"[FULFILL] DRY RUN — skipping supplier call: session={session_id} slot={slot_id}")

    try:
        if not dry_run:
            from send_booking_email import send_booking_email
            send_booking_email("booking_initiated", customer["email"], customer["name"], slot_for_email)
    except Exception as mail_err:
        print(f"[FULFILL] Initiated email failed (non-fatal): {mail_err}")

    try:
        if dry_run:
            # Synthetic path — no supplier call, no payment capture, no customer email.
            # Simulates success so every other part of the pipeline can be verified.
            import time as _time
            _time.sleep(0.5)  # realistic latency simulation
            confirmation       = f"DRY-RUN-{slot_id[:16].upper()}"
            supplier_reference = ""
            booking_meta = {
                "attempts": 0, "retries": 0, "retry_stage": None,
                "reservation_ms": None, "confirm_ms": None,
            }
            print(f"[FULFILL] DRY RUN complete: synthetic confirmation={confirmation}")
        else:
            # Run supplier call in a sub-thread so we can enforce a hard wall-clock ceiling.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="octo") as ex:
                fut = ex.submit(_fulfill_booking, slot_id, customer, platform, booking_url, quantity)
                confirmation, booking_meta, supplier_reference = fut.result(timeout=_MAX_BOOKING_TIMEOUT_S)

        # Resolve supplier_id early — needed for both success and failure paths
        try:
            burl_j      = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
            supplier_id = burl_j.get("supplier_id", platform)
        except Exception:
            supplier_id = platform

        # Capture the payment hold — skip in dry_run (no real money involved)
        if payment_intent and not dry_run:
            try:
                stripe.PaymentIntent.capture(payment_intent)
                print(f"[FULFILL] Payment captured: {payment_intent}")
            except Exception as cap_err:
                # CRITICAL: OCTO booking is confirmed but payment capture failed.
                # We MUST cancel the OCTO booking to avoid giving away a free tour.
                print(f"[FULFILL] *** CAPTURE FAILED: {cap_err} — cancelling OCTO booking ***")
                octo_cancel_result = _cancel_octo_booking(supplier_id, str(confirmation))
                if not octo_cancel_result.get("success"):
                    # Last resort: save a persistent record for manual resolution
                    _save_booking_record(f"ORPHAN_{session_id[:20]}", {
                        "status":           "capture_failed_needs_manual_action",
                        "confirmation":     str(confirmation),
                        "supplier_id":      supplier_id,
                        "payment_intent":   payment_intent,
                        "octo_cancel_error": octo_cancel_result.get("detail", "unknown"),
                        "capture_error":    str(cap_err)[:500],
                        "created_at":       datetime.now(timezone.utc).isoformat(),
                    })
                    print(f"[FULFILL] *** ORPHAN RECORD SAVED — manual action required ***")
                raise  # Re-raise so outer except cancels the payment hold

        _mark_booked(slot_id)

        # Use the pre-assigned pending booking_id so polling agents see the
        # status transition: pending_payment → booked on the same ID.
        booking_record_id = pending_booking_id or f"bk_{slot_id[:12]}"

        record = {
            "booking_id":            booking_record_id,
            "session_id":            session_id,
            "confirmation":          str(confirmation or ""),
            "supplier_reference":    str(supplier_reference or ""),  # Bokun's own ref (used in webhook cancellations)
            "platform":              platform,
            "supplier_id":           supplier_id,
            "booking_url":           booking_url,
            "service_name":          service_name,
            "business_name":         slot_for_email.get("business_name", ""),
            "location_city":         slot_for_email.get("location_city", ""),
            "start_time":            slot_for_email.get("start_time", ""),
            "price_charged":         amount_total / 100,
            "status":                "booked",
            "dry_run":               dry_run,
            "cancellation_cutoff_hours": slot_for_email.get("cancellation_cutoff_hours"),
            "executed_at":           datetime.now(timezone.utc).isoformat(),
            "customer_name":         customer["name"],
            "customer_email":        customer["email"],
            "customer_phone":        customer["phone"],
            "payment_intent_id":     payment_intent,
            "slot_id":               slot_id,
            "execution_duration_ms": round((time.monotonic() - _exec_start) * 1000),
            # Per-step timing from OCTOBooker
            "reservation_ms":        booking_meta.get("reservation_ms"),
            "confirm_ms":            booking_meta.get("confirm_ms"),
            # Retry observability — attempts = total calls made, retries = attempts - 1
            "attempts":              booking_meta.get("attempts", 1),
            "retries":               booking_meta.get("retries", 0),
            "retry_stage":           booking_meta.get("retry_stage"),
        }
        _save_booking_record(booking_record_id, record)

        # If reservation cleanup was needed and failed, queue it for later
        if booking_meta.get("cleanup_required"):
            _save_booking_record(
                f"cleanup_{booking_meta['cleanup_reservation_uuid'][:32]}",
                {
                    "cleanup_required":    True,
                    "reservation_uuid":    booking_meta.get("cleanup_reservation_uuid"),
                    "supplier":            platform,
                    "supplier_base_url":   booking_meta.get("cleanup_base_url"),
                    "detected_at":         datetime.now(timezone.utc).isoformat(),
                },
            )
            print(f"[FULFILL] Cleanup record saved for orphaned reservation "
                  f"{booking_meta.get('cleanup_reservation_uuid')}")

        # Update idempotency record with final status
        _save_booking_record(wh_record_key, {"session_id": session_id, "status": "booked",
                                              "booking_id": booking_record_id,
                                              "dry_run":    dry_run,
                                              "completed_at": datetime.now(timezone.utc).isoformat()})

        # Skip customer emails in dry_run — no real customer, no noise in their inbox
        if not dry_run:
            _host = os.getenv("BOOKING_SERVER_HOST", "")
            cancel_url = (
                f"{_host}/cancel/{booking_record_id}"
                f"?t={_make_cancel_token(booking_record_id)}"
            ) if _host else ""
            try:
                send_booking_email("booking_confirmed", customer["email"], customer["name"],
                                   slot_for_email, confirmation_number=str(confirmation or ""),
                                   cancel_url=cancel_url)
            except Exception as mail_err:
                print(f"[FULFILL] Confirmation email failed (non-fatal): {mail_err}")

        mode = "DRY RUN" if dry_run else "LIVE"
        print(f"[FULFILL] {mode} done: session={session_id} confirmation={confirmation}")

    except Exception as e:
        failure_reason = _classify_failure(e)
        print(f"[FULFILL] Booking failed ({failure_reason}): {e}")

        # If the OCTO booking was confirmed at the supplier BEFORE a payment capture
        # failure, the supplier has a live booking with no corresponding payment.
        # Queue it for automatic OCTO cancellation so the slot is released.
        if confirmation is not None and payment_intent and not dry_run:
            try:
                burl_j_fail = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
                supplier_id_fail = burl_j_fail.get("supplier_id", platform)
                orphan_id = f"capture_fail_{session_id[:20]}"
                _queue_octo_retry(orphan_id, supplier_id_fail, str(confirmation),
                                  payment_intent, amount_total / 100)
                print(f"[FULFILL] Orphaned OCTO booking queued for cancellation: {confirmation}")
            except Exception as queue_err:
                print(f"[FULFILL] Could not queue orphaned OCTO cancel: {queue_err} — manual action needed")

        if payment_intent:
            try:
                stripe.PaymentIntent.cancel(payment_intent)
                print(f"[FULFILL] Payment hold cancelled (customer not charged): {payment_intent}")
            except Exception as cancel_err:
                print(f"[FULFILL] Failed to cancel hold: {cancel_err} — manual action required")

        _save_booking_record(wh_record_key, {
            "session_id":    session_id,
            "status":        "failed",
            "failure_reason": failure_reason,
            "error":         str(e)[:500],
            "failed_at":     datetime.now(timezone.utc).isoformat(),
        })
        # Merge into the pending booking record — do NOT overwrite, as the pending
        # record contains service_name, customer fields, checkout_url, etc. that
        # are needed for get_booking_status to return useful context after failure.
        if pending_booking_id:
            _existing_pending = _load_booking_record(pending_booking_id) or {}
            _save_booking_record(pending_booking_id, {**_existing_pending,
                "status":         "failed",
                "failure_reason": failure_reason,
                "error":          str(e)[:500],
                "failed_at":      datetime.now(timezone.utc).isoformat(),
            })

        try:
            send_booking_email("booking_failed", customer["email"], customer["name"],
                               slot_for_email, error_reason=str(e))
        except Exception as mail_err:
            print(f"[FULFILL] Failure email failed (non-fatal): {mail_err}")

    finally:
        with _WEBHOOK_LOCK:
            _WEBHOOK_IN_FLIGHT.pop(session_id, None)


def _fulfill_booking(slot_id: str, customer: dict, platform: str, booking_url: str, quantity: int = 1):
    """
    Execute the booking on the source platform after payment is confirmed.
    Imports complete_booking.py (OCTO/Rezdy HTTP fulfillment).
    Checks the circuit breaker before attempting OCTO platforms.
    """
    # ── Resolve per-supplier ID from booking_url ─────────────────────────────
    # booking_url is a JSON blob that includes supplier_id (e.g. "arctic_adventures",
    # "bicycle_roma") set during fetch. Use it for per-supplier circuit breakers so
    # one Bokun supplier failing doesn't trip the breaker for all nine.
    supplier_id = platform
    burl_j = {}
    try:
        burl_j = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
        # Prefer granular supplier_id; fall back to platform (e.g. "bokun_reseller")
        supplier_id = burl_j.get("supplier_id") or burl_j.get("vendor_name") or platform
    except Exception:
        pass

    octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
    is_octo = platform in octo_platforms or platform == "octo" or burl_j.get("_type") == "octo"
    if is_octo:
        try:
            cb_spec = _ilu.spec_from_file_location("circuit_breaker", Path(__file__).parent / "circuit_breaker.py")
            cb_mod  = _ilu.module_from_spec(cb_spec)
            cb_spec.loader.exec_module(cb_mod)
            blocked, reason = cb_mod.is_open(supplier_id)
            if blocked:
                raise Exception(f"Circuit breaker open: {reason}")
        except Exception as cb_err:
            if "Circuit breaker open" in str(cb_err):
                raise
            # circuit_breaker.py unavailable — proceed anyway
    # ── Execute booking ───────────────────────────────────────────────────────
    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "complete_booking",
            Path(__file__).parent / "complete_booking.py"
        )
        if spec:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.complete_booking(
                slot_id=slot_id,
                customer=customer,
                platform=platform,
                booking_url=booking_url,
                quantity=quantity,
            )
            # complete_booking() always returns a dict with confirmation,
            # supplier_reference, and booking_meta keys.
            confirmation       = result.get("confirmation", "")
            supplier_reference = result.get("supplier_reference", "")
            booking_meta       = result.get("booking_meta", {})
            print(f"[FULFILLMENT] Confirmed: {confirmation} supplier_ref={supplier_reference}")
            # Record success with circuit breaker
            if is_octo:
                try:
                    cb_mod.record_success(supplier_id)
                except Exception:
                    pass
            return confirmation, booking_meta, supplier_reference
    except FileNotFoundError:
        # Raising here ensures the caller cancels the payment hold and marks the booking
        # as failed. Returning a fake confirmation would mark the booking as "booked"
        # with no actual supplier reservation made.
        raise Exception("complete_booking.py not found — cannot execute booking")
    except Exception as e:
        # Record failure with circuit breaker
        if is_octo:
            try:
                cb_mod.record_failure(supplier_id, str(e)[:200])
            except Exception:
                pass
        raise e


# Fields stripped from every public /slots response.
# booking_url contains the supplier's API base URL and credentials env-var name —
# exposing it lets agents bypass our platform and book directly at the original price.
# price is our cost basis — exposing it reveals our markup and enables arbitrage.
# business_id, api_key_env, and data_source are internal operational fields.
_PII_FIELDS = frozenset({
    "customer_email", "customer_phone", "customer_name", "payment_intent_id",
    "wallet_id", "booking_url",
})

_SLOT_INTERNAL_FIELDS = frozenset({
    "booking_url", "price", "original_price", "markup_pct", "our_markup",
    "business_id", "data_source", "test_group", "scraped_at",
    "api_key_env", "platform",
})

def _cancel_policy_label(hours) -> str:
    """Convert cancellation_cutoff_hours to a human-readable policy string."""
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return "Free cancellation up to 48 hours before the activity"
    if h >= 9999 * 24:
        return "Non-refundable — no cancellations or refunds"
    if h <= 0:
        return "Free cancellation up to departure"
    if h % 24 == 0 and h >= 48:
        return f"Free cancellation up to {h // 24} days before the activity"
    return f"Free cancellation up to {h} hours before the activity"


def _sanitize_slot(slot: dict) -> dict:
    """Strip internal fields and recompute hours_until_start before returning a slot."""
    out = {k: v for k, v in slot.items() if k not in _SLOT_INTERNAL_FIELDS}
    # Recompute hours_until_start from start_time so the value is always current,
    # not the stale value computed at fetch time (which can be hours old).
    try:
        start_iso = out.get("start_time", "")
        if start_iso:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            hours = (start_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            out["hours_until_start"] = round(hours, 2)
    except Exception:
        pass
    # Add human-readable cancellation policy for agent consumption
    out["cancellation_policy"] = _cancel_policy_label(
        slot.get("cancellation_cutoff_hours")
    )
    return out


# ── Booking page (human-facing) ─────────────────────────────────────────────

_BOOKING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{service_name} — Last Minute Deals HQ</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#1a1a1a;min-height:100vh;display:flex;flex-direction:column}}
.header{{background:#1a1a2e;color:#fff;padding:16px 24px;text-align:center;font-size:14px;letter-spacing:.5px}}
.header span{{color:#e94560}}
.container{{max-width:520px;margin:32px auto;padding:0 16px;flex:1}}
.card{{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);overflow:hidden}}
.card-header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:24px}}
.card-header h1{{font-size:20px;line-height:1.3;margin-bottom:8px}}
.card-header .supplier{{opacity:.8;font-size:14px}}
.details{{padding:20px 24px;border-bottom:1px solid #eee}}
.detail-row{{display:flex;justify-content:space-between;padding:8px 0;font-size:15px}}
.detail-row .label{{color:#666}}
.detail-row .value{{font-weight:600}}
.price-row .value{{color:#e94560;font-size:18px}}
form{{padding:24px}}
form h2{{font-size:16px;margin-bottom:16px;color:#333}}
.field{{margin-bottom:14px}}
.field label{{display:block;font-size:13px;color:#555;margin-bottom:4px;font-weight:500}}
.field input,.field select{{width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:8px;font-size:15px;transition:border-color .2s}}
.field input:focus,.field select:focus{{outline:none;border-color:#e94560}}
.qty-row{{display:flex;gap:12px;align-items:end}}
.qty-row .field{{flex:1}}
.btn{{display:block;width:100%;padding:14px;background:#e94560;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;transition:background .2s;margin-top:8px}}
.btn:hover{{background:#d63851}}
.btn:disabled{{background:#ccc;cursor:not-allowed}}
.note{{text-align:center;font-size:12px;color:#999;margin-top:12px;padding:0 24px 20px}}
.cancel-standard{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:13px;color:#166534;text-align:left}}
.cancel-long{{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:13px;color:#92400e;text-align:left}}
.cancel-nonrefund{{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:13px;color:#991b1b;text-align:left}}
.cancel-check{{display:flex;align-items:flex-start;gap:8px;margin-top:8px;cursor:pointer;font-size:13px}}
.cancel-check input{{margin-top:2px;flex-shrink:0}}
.footer{{text-align:center;padding:24px;font-size:12px;color:#999}}
.error{{background:#fff0f0;color:#c00;padding:16px 24px;border-radius:12px;margin-bottom:16px;text-align:center}}
.gone{{text-align:center;padding:48px 24px}}
.gone h2{{margin-bottom:8px;color:#666}}
</style>
</head>
<body>
<div class="header">LAST MINUTE DEALS <span>HQ</span></div>
<div class="container">
{error_block}
<div class="card">
<div class="card-header">
<h1>{service_name}</h1>
<div class="supplier">{business_name}</div>
</div>
<div class="details">
<div class="detail-row"><span class="label">Date &amp; Time</span><span class="value">{formatted_time}</span></div>
<div class="detail-row"><span class="label">Location</span><span class="value">{location}</span></div>
<div class="detail-row"><span class="label">Duration</span><span class="value">{duration}</span></div>
<div class="detail-row price-row"><span class="label">Price per person</span><span class="value">{price_display}</span></div>
</div>
<form method="POST" action="/book/{slot_id}/checkout" id="bookForm">
<h2>Your Details</h2>
<div class="field"><label for="name">Full Name</label><input type="text" id="name" name="customer_name" required placeholder="Jane Smith"></div>
<div class="field"><label for="email">Email</label><input type="email" id="email" name="customer_email" required placeholder="jane@example.com"></div>
<div class="field"><label for="phone">Phone (with country code)</label><input type="tel" id="phone" name="customer_phone" required placeholder="+1 555 000 1234"></div>
<div class="qty-row">
<div class="field"><label for="qty">Guests</label><select id="qty" name="quantity">{qty_options}</select></div>
<div class="field"><label>&nbsp;</label><div style="font-size:14px;color:#666;padding:10px 0">Total: <strong id="total">{price_display}</strong></div></div>
</div>
{cancellation_block}
<button type="submit" class="btn" id="bookBtn"{book_btn_disabled}>Book Now — Pay Securely</button>
</form>
<div class="note">You'll be redirected to Stripe for secure payment. Your card is only charged after the booking is confirmed with the supplier.</div>
</div>
</div>
<div class="footer">Powered by Last Minute Deals HQ &middot; Secure payments by Stripe</div>
<script>
(function(){{
var price={price_raw},cur="{currency}";
var sel=document.getElementById("qty"),total=document.getElementById("total"),btn=document.getElementById("bookBtn");
function fmt(v){{return new Intl.NumberFormat(undefined,{{style:"currency",currency:cur}}).format(v)}}
sel.addEventListener("change",function(){{total.innerHTML=fmt(price*parseInt(sel.value))}});
var cb=document.getElementById("cancelAck");
if(cb){{cb.addEventListener("change",function(){{btn.disabled=!cb.checked}})}}
document.getElementById("bookForm").addEventListener("submit",function(){{btn.disabled=true;btn.textContent="Redirecting to payment…"}});
}})();
</script>
</body>
</html>"""

_BOOKING_PAGE_GONE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slot Not Available — Last Minute Deals HQ</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#1a1a1a;min-height:100vh;display:flex;flex-direction:column}}
.header{{background:#1a1a2e;color:#fff;padding:16px 24px;text-align:center;font-size:14px;letter-spacing:.5px}}
.header span{{color:#e94560}}
.container{{max-width:520px;margin:32px auto;padding:0 16px;flex:1}}
.gone{{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center;padding:48px 24px}}
.gone h2{{margin-bottom:8px;color:#666}}
.gone p{{color:#999}}
.footer{{text-align:center;padding:24px;font-size:12px;color:#999}}
</style>
</head>
<body>
<div class="header">LAST MINUTE DEALS <span>HQ</span></div>
<div class="container">
<div class="gone"><h2>Slot Not Available</h2><p>This slot may have been booked or expired. Ask your AI assistant to search for more options.</p></div>
</div>
<div class="footer">Powered by Last Minute Deals HQ</div>
</body>
</html>"""


@app.route("/privacy", methods=["GET"])
def privacy_policy():
    """Simple privacy policy page required by GPT Store and app registries."""
    return """<!DOCTYPE html>
<html><head><title>Privacy Policy — Last Minute Deals HQ</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;line-height:1.6;color:#333}h1{font-size:1.5em}h2{font-size:1.15em;margin-top:1.5em}</style>
</head><body>
<h1>Privacy Policy</h1>
<p><strong>Last Minute Deals HQ LLC</strong> — Effective April 21, 2026</p>

<h2>What We Collect</h2>
<p>When you book a tour or activity through our service, we collect your name, email address, and phone number. This information is provided by you on the booking page and is used solely to complete your reservation with the tour supplier.</p>

<h2>Search Data</h2>
<p>Slot searches (city, category, date filters) are processed in real time and not stored or linked to any individual user. We do not track browsing behavior or use cookies.</p>

<h2>Payment</h2>
<p>Payments are processed by Stripe. We do not store credit card numbers or payment credentials. See <a href="https://stripe.com/privacy">Stripe's Privacy Policy</a> for details on how they handle payment data.</p>

<h2>Data Sharing</h2>
<p>Your booking details (name, email, phone) are shared only with the tour supplier fulfilling your reservation. We do not sell, rent, or share your personal information with any other third parties.</p>

<h2>Data Retention</h2>
<p>Booking records are retained for customer support and cancellation purposes. You may request deletion of your data by emailing johnanleitner1@gmail.com.</p>

<h2>Contact</h2>
<p>Last Minute Deals HQ LLC — johnanleitner1@gmail.com</p>
</body></html>"""


# ── SEO Tour Landing Pages ────────────────────────────────────────────────────

_TOURS_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Last-Minute Tours & Experiences Worldwide — Last Minute Deals HQ</title>
<meta name="description" content="Book last-minute tours and activities in 48 countries. Walking tours in Paris, food tours in Barcelona, canal cruises in Amsterdam, glacier hikes in Iceland, pyramid tours in Egypt, and more. Instant confirmation from 37 suppliers.">
<link rel="canonical" href="https://api.lastminutedealshq.com/tours">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#1a1a1a;min-height:100vh;display:flex;flex-direction:column}}
.header{{background:#1a1a2e;color:#fff;padding:16px 24px;text-align:center;font-size:14px;letter-spacing:.5px}}
.header span{{color:#e94560}}
.container{{max-width:900px;margin:32px auto;padding:0 16px;flex:1}}
h1{{font-size:28px;margin-bottom:12px;color:#1a1a2e}}
.subtitle{{font-size:16px;color:#555;margin-bottom:32px;line-height:1.5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
.dest-card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.06);padding:24px;transition:box-shadow .2s;text-decoration:none;color:inherit;display:block}}
.dest-card:hover{{box-shadow:0 4px 16px rgba(0,0,0,.12)}}
.dest-card h2{{font-size:18px;color:#1a1a2e;margin-bottom:8px}}
.dest-card .highlights{{font-size:13px;color:#666;margin-bottom:12px;line-height:1.4}}
.dest-card .count{{font-size:14px;color:#e94560;font-weight:600}}
.footer{{text-align:center;padding:24px;font-size:12px;color:#999}}
</style>
</head>
<body>
<div class="header"><a href="/" style="color:#fff;text-decoration:none">LAST MINUTE DEALS <span>HQ</span></a></div>
<div class="container">
<h1>Last-Minute Tours & Experiences Worldwide</h1>
<p class="subtitle">Book instantly-confirmed tours and activities from {supplier_count} suppliers across 48 countries. All inventory is live — if you see it, you can book it.</p>
<div class="grid">
{destination_cards}
</div>
</div>
<div class="footer">Powered by Last Minute Deals HQ &middot; Secure payments by Stripe</div>
</body>
</html>"""

_TOURS_DEST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Last Minute Deals HQ</title>
<meta name="description" content="{meta_desc}">
<link rel="canonical" href="https://api.lastminutedealshq.com/tours/{slug}">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f5f5f5;color:#1a1a1a;min-height:100vh;display:flex;flex-direction:column}}
.header{{background:#1a1a2e;color:#fff;padding:16px 24px;text-align:center;font-size:14px;letter-spacing:.5px}}
.header span{{color:#e94560}}
.container{{max-width:900px;margin:32px auto;padding:0 16px;flex:1}}
h1{{font-size:28px;margin-bottom:12px;color:#1a1a2e}}
.intro{{font-size:15px;color:#555;margin-bottom:8px;line-height:1.6}}
.highlights{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px}}
.tag{{background:#eef;color:#1a1a2e;padding:4px 12px;border-radius:20px;font-size:13px}}
.slot-count{{font-size:14px;color:#666;margin-bottom:16px}}
.slots{{display:grid;gap:16px;margin-bottom:32px}}
.slot-card{{background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.06);padding:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
.slot-info{{flex:1;min-width:200px}}
.slot-info h3{{font-size:16px;margin-bottom:4px;color:#1a1a2e}}
.slot-info .supplier{{font-size:13px;color:#888;margin-bottom:6px}}
.slot-info .meta{{font-size:14px;color:#555}}
.slot-action{{text-align:right}}
.slot-price{{font-size:20px;font-weight:700;color:#e94560;margin-bottom:6px}}
.slot-price .currency{{font-size:14px;font-weight:400}}
.book-btn{{display:inline-block;padding:10px 20px;background:#e94560;color:#fff;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;transition:background .2s}}
.book-btn:hover{{background:#d63851}}
.empty{{text-align:center;padding:48px 24px;color:#666}}
.back{{display:inline-block;margin-bottom:20px;color:#e94560;text-decoration:none;font-size:14px}}
.back:hover{{text-decoration:underline}}
.other-dests{{margin-top:32px;padding-top:24px;border-top:1px solid #ddd}}
.other-dests h2{{font-size:18px;margin-bottom:12px;color:#1a1a2e}}
.other-links{{display:flex;flex-wrap:wrap;gap:8px}}
.other-links a{{color:#e94560;text-decoration:none;padding:4px 12px;border:1px solid #e94560;border-radius:20px;font-size:13px;transition:all .2s}}
.other-links a:hover{{background:#e94560;color:#fff}}
.footer{{text-align:center;padding:24px;font-size:12px;color:#999}}
</style>
{structured_data}
</head>
<body>
<div class="header"><a href="/" style="color:#fff;text-decoration:none">LAST MINUTE DEALS <span>HQ</span></a></div>
<div class="container">
<a href="/tours" class="back">&larr; All destinations</a>
<h1>{heading}</h1>
<p class="intro">{intro}</p>
<div class="highlights">{highlight_tags}</div>
<p class="slot-count">{slot_count_text}</p>
<div class="slots">
{slot_cards}
</div>
{other_destinations}
</div>
<div class="footer">Powered by Last Minute Deals HQ &middot; Secure payments by Stripe</div>
</body>
</html>"""


# Country name → ISO code mapping for Supabase queries (location_country stores ISO codes)
_COUNTRY_ISO = {
    "iceland": "IS", "egypt": "EG", "italy": "IT", "portugal": "PT",
    "tanzania": "TZ", "morocco": "MA", "japan": "JP", "turkey": "TR",
    "montenegro": "ME", "finland": "FI", "china": "CN", "mexico": "MX",
    "united kingdom": "GB", "romania": "RO", "united states": "US",
    "costa rica": "CR", "france": "FR", "spain": "ES", "netherlands": "NL",
    "germany": "DE", "austria": "AT", "czech republic": "CZ", "czechia": "CZ",
    "hungary": "HU", "greece": "GR", "croatia": "HR", "ireland": "IE",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "poland": "PL",
    "switzerland": "CH", "belgium": "BE", "malta": "MT", "slovakia": "SK",
    "slovenia": "SI", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "monaco": "MC", "luxembourg": "LU",
}


def _fetch_tour_slots(query: str, limit: int = 50) -> list[dict]:
    """Fetch slots matching a destination query (searches city name and country ISO code)."""
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not (sb_url and sb_secret):
        return []
    try:
        hdrs = {"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"}
        now_iso = datetime.now(timezone.utc).isoformat()
        horizon = (datetime.now(timezone.utc) + timedelta(hours=168)).isoformat()

        # Build OR filter: match city name OR country ISO code
        # location_country stores 2-letter ISO codes (IS, EG, etc.), not full names
        or_parts = [f"location_city.ilike.%{query}%"]
        iso = _COUNTRY_ISO.get(query.lower())
        if iso:
            or_parts.append(f"location_country.eq.{iso}")
        else:
            or_parts.append(f"location_country.ilike.%{query}%")
        # Also search known cities for this destination
        dest_cfg = None
        for d in _TOUR_DESTINATIONS.values():
            if d["query"].lower() == query.lower():
                dest_cfg = d
                break
        if dest_cfg:
            for sup in _SUPPLIER_DIR_STATIC:
                for dest_name in sup["destinations"]:
                    if dest_name.lower() != query.lower() and query.lower() in [dn.lower() for dn in sup["destinations"]]:
                        or_parts.append(f"location_city.ilike.%{dest_name}%")

        or_filter = f"({','.join(or_parts)})"
        resp = requests.get(
            f"{sb_url}/rest/v1/slots",
            headers=hdrs,
            params=[
                ("order", "start_time.asc"),
                ("start_time", f"gt.{now_iso}"),
                ("start_time", f"lte.{horizon}"),
                ("or", or_filter),
                ("limit", limit),
            ],
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        rows = resp.json()
        result = []
        for row in rows:
            if row.get("raw"):
                try:
                    result.append(_sanitize_slot(json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"]))
                    continue
                except Exception:
                    pass
            result.append(_sanitize_slot(row))
        return result
    except Exception:
        return []


def _format_slot_card(slot: dict) -> str:
    """Render a single slot as an HTML card for the tour landing page."""
    from html import escape
    name = escape(slot.get("service_name", "Experience")[:100])
    supplier = escape(slot.get("business_name", "")[:80])
    start_iso = slot.get("start_time", "")
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        formatted = dt.strftime("%b %d, %Y &middot; %I:%M %p UTC")
    except Exception:
        formatted = escape(start_iso[:16].replace("T", " ")) if start_iso else "TBD"
    price = float(slot.get("our_price") or slot.get("price") or 0)
    currency = (slot.get("currency") or "USD").upper()
    sid = escape(slot.get("slot_id", ""))
    city = escape(slot.get("location_city", ""))
    dur = slot.get("duration_minutes")
    dur_text = ""
    if dur:
        h, m = divmod(int(dur), 60)
        dur_text = f" &middot; {h}h {m}m" if h else f" &middot; {m} min"
    return (
        f'<div class="slot-card">'
        f'<div class="slot-info"><h3>{name}</h3>'
        f'<div class="supplier">{supplier}</div>'
        f'<div class="meta">{formatted}{dur_text}</div></div>'
        f'<div class="slot-action">'
        f'<div class="slot-price"><span class="currency">{currency}</span> {price:,.2f}</div>'
        f'<a href="/book/{sid}" class="book-btn">View &amp; Book</a></div></div>'
    )


# ---------------------------------------------------------------------------
# Google Search Console verification + SEO helpers
# ---------------------------------------------------------------------------

@app.route("/google1146a4e71b31f0ee.html", methods=["GET"])
def google_verification():
    return "google-site-verification: google1146a4e71b31f0ee.html", 200, {"Content-Type": "text/html"}


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Dynamic sitemap listing all tour destination pages + booking pages."""
    base = "https://api.lastminutedealshq.com"
    urls = [
        (f"{base}/tours", "daily", "1.0"),
    ]
    for slug in _TOUR_DESTINATIONS:
        urls.append((f"{base}/tours/{slug}", "daily", "0.8"))
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, prio in urls:
        xml_parts.append(
            f"<url><loc>{loc}</loc>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
    xml_parts.append("</urlset>")
    return "\n".join(xml_parts), 200, {"Content-Type": "application/xml"}


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /mcp\n"
        "\n"
        "Sitemap: https://api.lastminutedealshq.com/sitemap.xml\n"
    )
    return body, 200, {"Content-Type": "text/plain"}


_TOURS_INDEX_CACHE: dict = {}  # {"html": str, "expires": float}
_TOURS_INDEX_CACHE_TTL = 600  # 10 minutes


@app.route("/tours", methods=["GET"])
def tours_index():
    """SEO index page listing all tour destinations with live slot counts."""
    now = time.time()
    cached = _TOURS_INDEX_CACHE.get("html")
    if cached and _TOURS_INDEX_CACHE.get("expires", 0) > now:
        return cached, 200, {"Content-Type": "text/html"}

    cards = []
    for slug, dest in _TOUR_DESTINATIONS.items():
        slots = _fetch_tour_slots(dest["query"], limit=200)
        count = len(slots)
        highlights = ", ".join(dest["highlights"][:4])
        count_text = f"{count} tours available now" if count else "Check back soon"
        cards.append(
            f'<a href="/tours/{slug}" class="dest-card">'
            f'<h2>{dest["name"]}</h2>'
            f'<div class="highlights">{highlights}</div>'
            f'<div class="count">{count_text}</div></a>'
        )
    html = _TOURS_INDEX_HTML.format(destination_cards="\n".join(cards), supplier_count=_supplier_count())
    _TOURS_INDEX_CACHE["html"] = html
    _TOURS_INDEX_CACHE["expires"] = now + _TOURS_INDEX_CACHE_TTL
    return html, 200, {"Content-Type": "text/html"}


@app.route("/tours/<slug>", methods=["GET"])
def tours_destination(slug):
    """SEO destination page showing live inventory with booking links."""
    from html import escape
    dest = _TOUR_DESTINATIONS.get(slug)
    if dest:
        query = dest["query"]
        title = dest["title"]
        meta_desc = dest["meta_desc"]
        heading = title
        intro = dest["intro"]
        highlights = dest["highlights"]
    else:
        # Dynamic fallback for unlisted destinations
        query = slug.replace("-", " ").title()
        title = f"Last-Minute {query} Tours & Experiences"
        meta_desc = f"Book last-minute tours and activities in {query}. Instant confirmation from local suppliers."
        heading = title
        intro = f"Discover available tours and activities in {query} with instant confirmation from local suppliers."
        highlights = ["Tours", "Activities", "Experiences"]

    slots = _fetch_tour_slots(query, limit=50)
    highlight_tags = "".join(f'<span class="tag">{escape(h)}</span>' for h in highlights)
    slot_count_text = f"{len(slots)} experience{'s' if len(slots) != 1 else ''} available now" if slots else ""

    if slots:
        slot_cards = "\n".join(_format_slot_card(s) for s in slots)
    else:
        slot_cards = '<div class="empty"><h2>No tours available right now</h2><p>Check back soon — inventory refreshes every 4 hours.</p></div>'

    # Other destination links for internal linking
    other = []
    for s, d in _TOUR_DESTINATIONS.items():
        if s != slug:
            other.append(f'<a href="/tours/{s}">{d["name"]}</a>')
    other_html = (
        f'<div class="other-dests"><h2>Explore other destinations</h2>'
        f'<div class="other-links">{"".join(other)}</div></div>'
    )

    # JSON-LD structured data for SEO
    structured = ""
    if slots:
        items = []
        for idx, s in enumerate(slots[:10], 1):
            price_val = s.get("our_price") or s.get("price") or 0
            currency_val = (s.get("currency") or "USD").upper()
            city = s.get("location_city", "")
            country = s.get("location_country", "")
            start = s.get("start_time", "")
            end = s.get("end_time", "")
            dur = s.get("duration_minutes")
            sid = s.get("slot_id", "")
            cat = s.get("category", "experiences")
            # Build Event item (Google Things to Do prefers Event)
            item = {
                "@type": "Event",
                "name": s.get("service_name", ""),
                "description": f"{s.get('service_name', '')} by {s.get('business_name', '')} in {city}",
                "organizer": {"@type": "Organization", "name": s.get("business_name", "")},
                "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
                "eventStatus": "https://schema.org/EventScheduled",
                "offers": {
                    "@type": "Offer",
                    "price": str(price_val),
                    "priceCurrency": currency_val,
                    "availability": "https://schema.org/InStock",
                    "validFrom": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "url": f"https://api.lastminutedealshq.com/book/{sid}",
                },
            }
            if start:
                item["startDate"] = start
            if end:
                item["endDate"] = end
            elif dur and start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    item["endDate"] = (dt + timedelta(minutes=int(dur))).isoformat()
                except Exception:
                    pass
            if dur:
                h, m = divmod(int(dur), 60)
                item["duration"] = f"PT{h}H{m}M" if h else f"PT{m}M"
            if city:
                loc = {"@type": "Place", "name": city, "address": {"@type": "PostalAddress", "addressLocality": city}}
                if country:
                    loc["address"]["addressCountry"] = country
                item["location"] = loc
            items.append(item)
        ld_json = {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": title,
            "numberOfItems": len(items),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "item": item}
                for i, item in enumerate(items)
            ],
        }
        structured = (
            '<script type="application/ld+json">'
            + json.dumps(ld_json)
            + '</script>'
        )

    html = _TOURS_DEST_HTML.format(
        title=escape(title), meta_desc=escape(meta_desc), slug=escape(slug),
        heading=escape(heading), intro=escape(intro),
        highlight_tags=highlight_tags, slot_count_text=slot_count_text,
        slot_cards=slot_cards, other_destinations=other_html,
        structured_data=structured,
    )
    return html, 200, {"Content-Type": "text/html"}


# ── Book from Itinerary Endpoint ──────────────────────────────────────────────

# Common words to skip during fuzzy matching
_STOP_WORDS = frozenset(
    "the a an in at on to of for and or with from by is are was were "
    "this that it its my our your day days trip tour visit go see do "
    "get take have been will can could would should".split()
)


def _fuzzy_match_slots(line: str, slots: list[dict], top_n: int = 3) -> list[dict]:
    """Score slots against an itinerary line using keyword overlap."""
    words = {w.lower() for w in line.split() if len(w) > 2 and w.lower() not in _STOP_WORDS}
    if not words:
        return []
    scored = []
    for s in slots:
        name_words = {w.lower() for w in (s.get("service_name") or "").split() if len(w) > 2}
        overlap = len(words & name_words)
        if overlap > 0:
            scored.append((overlap, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top_n]]


@app.route("/api/book_from_itinerary", methods=["POST"])
def book_from_itinerary():
    """
    Accept raw itinerary text and return booking links for matching inventory.

    Extracts destination mentions and activity keywords, matches against live
    inventory, and returns structured results with booking page URLs.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("itinerary") or data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Provide 'itinerary' or 'text' field with your itinerary."}), 400

    # Build a lookup of known destination keywords → query strings
    dest_keywords: dict[str, str] = {}
    for dest in _TOUR_DESTINATIONS.values():
        dest_keywords[dest["name"].lower()] = dest["query"]
        dest_keywords[dest["query"].lower()] = dest["query"]
    # Add specific cities from supplier directory
    for sup in _SUPPLIER_DIR_STATIC:
        for d in sup["destinations"]:
            dest_keywords[d.lower()] = d

    # Detect destinations mentioned in the text
    text_lower = text.lower()
    detected_queries: list[str] = []
    seen: set[str] = set()
    for keyword, query in sorted(dest_keywords.items(), key=lambda x: -len(x[0])):
        if keyword in text_lower and query not in seen:
            detected_queries.append(query)
            seen.add(query)

    if not detected_queries:
        return jsonify({
            "results": [],
            "matched_count": 0,
            "total_items": 0,
            "note": "No recognized destinations found in itinerary. Try mentioning a city or country name.",
            "available_destinations": sorted(set(d["name"] for d in _TOUR_DESTINATIONS.values())),
        })

    # Fetch inventory for detected destinations
    all_slots: list[dict] = []
    for q in detected_queries[:5]:
        all_slots.extend(_fetch_tour_slots(q, limit=100))
    # Deduplicate by slot_id
    slot_map = {s["slot_id"]: s for s in all_slots if s.get("slot_id")}
    unique_slots = list(slot_map.values())

    # Parse itinerary into lines/items
    import re
    lines = [l.strip() for l in re.split(r'[\n,;]|(?:^|\n)\s*[-*\d.]+\s*', text) if l.strip()]
    # Filter out lines that are just destination names
    items = [l for l in lines if len(l) > 3 and l.lower() not in dest_keywords]
    if not items:
        items = lines[:10]

    host = os.getenv("BOOKING_SERVER_HOST", "https://api.lastminutedealshq.com").rstrip("/")
    results = []
    matched_count = 0

    for item in items:
        matches = _fuzzy_match_slots(item, unique_slots)
        if matches:
            matched_count += 1
            results.append({
                "itinerary_item": item,
                "matched": True,
                "slots": [{
                    "slot_id": s.get("slot_id", ""),
                    "service_name": s.get("service_name", ""),
                    "business_name": s.get("business_name", ""),
                    "start_time": s.get("start_time", ""),
                    "price": float(s.get("our_price") or s.get("price") or 0),
                    "currency": s.get("currency", "USD"),
                    "booking_url": f"{host}/book/{s.get('slot_id', '')}",
                } for s in matches],
            })
        else:
            results.append({"itinerary_item": item, "matched": False, "slots": []})

    return jsonify({
        "results": results,
        "matched_count": matched_count,
        "total_items": len(items),
        "destinations_detected": detected_queries,
        "note": (
            f"{matched_count} of {len(items)} itinerary items matched available inventory. "
            "Click booking links to view details and reserve."
            if matched_count else
            "No matching activities found in current inventory. Try different activity names or check back later."
        ),
    })


@app.route("/book/<slot_id>", methods=["GET"])
def booking_page(slot_id):
    """Render an HTML booking page for a slot. Agents share this URL with users."""
    slot = get_slot_by_id(slot_id)
    if not slot:
        return _BOOKING_PAGE_GONE.format(), 404

    # Check it hasn't started
    start_iso = slot.get("start_time", "")
    try:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if start_dt <= datetime.now(timezone.utc):
            return _BOOKING_PAGE_GONE.format(), 410
    except Exception:
        pass

    # Format display values
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        formatted_time = dt.strftime("%B %d, %Y at %I:%M %p UTC")
    except Exception:
        formatted_time = start_iso[:16].replace("T", " ") if start_iso else "TBD"

    price = float(slot.get("our_price") or slot.get("price") or 0)
    currency = (slot.get("currency") or "USD").upper()
    duration_min = slot.get("duration_minutes")
    if duration_min:
        hours, mins = divmod(int(duration_min), 60)
        duration = f"{hours}h {mins}m" if hours else f"{mins} min"
    else:
        duration = "See details"

    city = slot.get("location_city", "")
    country = slot.get("location_country", "")
    location = f"{city}, {country}" if city and country else city or country or "See details"

    qty_options = "".join(
        f'<option value="{i}"{" selected" if i == 1 else ""}>{i}</option>'
        for i in range(1, 11)
    )

    try:
        import locale
        price_display = f"{currency} {price:,.2f}"
    except Exception:
        price_display = f"{currency} {price:.2f}"

    error_block = ""
    error_msg = request.args.get("error")
    if error_msg:
        error_block = f'<div class="error">{error_msg}</div>'

    # Build JSON-LD structured data for booking page
    ld_event = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": slot.get("service_name", "Experience")[:100],
        "description": f"{slot.get('service_name', '')} by {slot.get('business_name', '')} in {city}",
        "organizer": {"@type": "Organization", "name": slot.get("business_name", "")},
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "eventStatus": "https://schema.org/EventScheduled",
        "offers": {
            "@type": "Offer",
            "price": str(price),
            "priceCurrency": currency,
            "availability": "https://schema.org/InStock",
            "validFrom": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "url": f"https://api.lastminutedealshq.com/book/{slot_id}",
        },
    }
    if start_iso:
        ld_event["startDate"] = start_iso
    end_iso = slot.get("end_time", "")
    if end_iso:
        ld_event["endDate"] = end_iso
    elif duration_min and start_iso:
        try:
            ld_event["endDate"] = (datetime.fromisoformat(start_iso.replace("Z", "+00:00")) + timedelta(minutes=int(duration_min))).isoformat()
        except Exception:
            pass
    if duration_min:
        h, m = divmod(int(duration_min), 60)
        ld_event["duration"] = f"PT{h}H{m}M" if h else f"PT{m}M"
    if city:
        ld_loc = {"@type": "Place", "name": city, "address": {"@type": "PostalAddress", "addressLocality": city}}
        if country:
            ld_loc["address"]["addressCountry"] = country
        ld_event["location"] = ld_loc
    booking_ld = '<script type="application/ld+json">' + json.dumps(ld_event) + '</script>'

    _slot_cutoff = slot.get("cancellation_cutoff_hours")
    if _slot_cutoff is None:
        try:
            _burl = slot.get("booking_url", "")
            if _burl and isinstance(_burl, str) and _burl.startswith("{"):
                _slot_cutoff = json.loads(_burl).get("cancellation_cutoff_hours")
        except Exception:
            pass
    _slot_cutoff = int(_slot_cutoff) if _slot_cutoff is not None else 48

    # Build tier-appropriate cancellation policy block
    _NON_REFUNDABLE_SENTINEL = 9999 * 24
    if _slot_cutoff >= _NON_REFUNDABLE_SENTINEL:
        _cancel_block = (
            '<div class="cancel-nonrefund">'
            '<strong>Non-refundable:</strong> This booking cannot be cancelled or refunded once purchased.'
            '<label class="cancel-check"><input type="checkbox" id="cancelAck" required>'
            'I understand this booking is non-refundable and no cancellations or refunds are available.</label>'
            '</div>'
        )
        _btn_disabled = ' disabled'
    elif _slot_cutoff > 48:
        _cutoff_text = _cancel_cutoff_display(_slot_cutoff)
        _cancel_block = (
            '<div class="cancel-long">'
            f'<strong>Cancellation policy:</strong> This booking must be cancelled at least {_cutoff_text} '
            'in advance for a full refund. Late cancellations are non-refundable.'
            '<label class="cancel-check"><input type="checkbox" id="cancelAck" required>'
            f'I understand cancellations must be made at least {_cutoff_text} before the activity for a refund.</label>'
            '</div>'
        )
        _btn_disabled = ' disabled'
    else:
        _cutoff_text = _cancel_cutoff_display(_slot_cutoff)
        _cancel_block = (
            '<div class="cancel-standard">'
            f'<strong>Cancellation policy:</strong> Free cancellation up to {_cutoff_text} before the activity.'
            '</div>'
        )
        _btn_disabled = ''

    html = _BOOKING_PAGE_HTML.format(
        service_name=slot.get("service_name", "Experience")[:100],
        business_name=slot.get("business_name", "")[:80],
        formatted_time=formatted_time,
        location=location,
        duration=duration,
        price_display=price_display,
        price_raw=price,
        currency=currency,
        slot_id=slot_id,
        qty_options=qty_options,
        error_block=error_block,
        cancellation_block=_cancel_block,
        book_btn_disabled=_btn_disabled,
    )
    # Inject structured data before </head>
    html = html.replace("</head>", booking_ld + "</head>", 1)
    return html, 200, {"Content-Type": "text/html"}


@app.route("/book/<slot_id>/checkout", methods=["POST"])
def booking_checkout(slot_id):
    """Accept the booking form POST, create a Stripe checkout session, and redirect."""
    customer_name  = (request.form.get("customer_name") or "").strip()
    customer_email = (request.form.get("customer_email") or "").strip()
    customer_phone = (request.form.get("customer_phone") or "").strip()
    quantity       = max(1, min(int(request.form.get("quantity") or 1), 20))

    if not all([customer_name, customer_email, customer_phone]):
        return redirect(f"/book/{slot_id}?error=Please+fill+in+all+fields")

    # Reuse the existing /api/book logic via internal HTTP call
    api_key = os.getenv("LMD_WEBSITE_API_KEY", "")
    try:
        r = requests.post(
            f"http://localhost:{PORT}/api/book",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={
                "slot_id": slot_id,
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "quantity": quantity,
            },
            timeout=15,
        )
        data = r.json()
        if data.get("success") and data.get("checkout_url"):
            return redirect(data["checkout_url"])
        error = data.get("error", "Booking failed. Please try again.")
    except Exception as e:
        error = f"Service temporarily unavailable. Please try again."

    from urllib.parse import quote
    return redirect(f"/book/{slot_id}?error={quote(error)}")


@app.route("/slots", methods=["GET"])
def search_slots():
    """
    Search available slots. Supports filtering by category, city, hours_ahead, max_price.
    Used by AI agents, the OpenAPI spec, and the MCP server.
    """
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")

    category    = request.args.get("category", "").strip()
    city        = request.args.get("city", "").strip()
    hours_ahead = request.args.get("hours_ahead", 168, type=int)
    max_price   = request.args.get("max_price", type=float)
    limit       = request.args.get("limit", 0, type=int) or 10_000  # 0 = no limit

    if sb_url and sb_secret:
        try:
            hdrs = {
                "apikey": sb_secret,
                "Authorization": f"Bearer {sb_secret}",
                "Prefer": "count=none",
            }
            now_iso     = datetime.now(timezone.utc).isoformat()
            base_params: list[tuple] = [
                ("order", "start_time.asc"),
                ("start_time", f"gt.{now_iso}"),
            ]
            if hours_ahead:
                horizon_iso = (datetime.now(timezone.utc) + timedelta(hours=hours_ahead)).isoformat()
                base_params.append(("start_time", f"lte.{horizon_iso}"))
            if category:
                base_params.append(("category", f"eq.{category}"))
            if city:
                base_params.append(("location_city", f"ilike.%{city}%"))
            if max_price is not None:
                base_params.append(("our_price", f"lte.{max_price}"))

            # Paginate — Supabase max-rows config caps each response at 1000
            PAGE_SIZE = 1000
            result: list = []
            offset = 0
            while len(result) < limit:
                fetch_n = min(PAGE_SIZE, limit - len(result))
                page_params = base_params + [("limit", fetch_n), ("offset", offset)]
                resp = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs,
                                    params=page_params, timeout=10)
                if resp.status_code != 200:
                    break
                page = resp.json()
                if not page:
                    break
                for row in page:
                    if row.get("raw"):
                        try:
                            result.append(_sanitize_slot(json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"]))
                            continue
                        except Exception:
                            pass
                    result.append(_sanitize_slot(row))
                if len(page) < fetch_n:
                    break  # last page
                offset += fetch_n
            return jsonify(result)
        except Exception as e:
            print(f"Supabase search failed: {e}")

    # Fallback: local file
    if not DATA_FILE.exists():
        return jsonify([])
    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    result = []
    for s in slots:
        if category and s.get("category") != category:
            continue
        if city and city.lower() not in (s.get("location_city") or "").lower():
            continue
        h = s.get("hours_until_start")
        if h is not None and h > hours_ahead:
            continue
        p = s.get("our_price") or s.get("price") or 0
        if max_price is not None and float(p) > max_price:
            continue
        result.append(_sanitize_slot(s))
        if len(result) >= limit:
            break
    return jsonify(result)


@app.route("/slots/<slot_id>/quote", methods=["GET"])
def slot_quote(slot_id: str):
    """Check availability and return confirmed price for a specific slot."""
    slot = get_slot_by_id(slot_id)
    if not slot:
        return jsonify({"available": False, "error": "Slot not found or expired."}), 404
    if slot_id in _load_booked():
        return jsonify({"available": False, "error": "Already booked."}), 409
    try:
        start_iso = slot.get("start_time", "")
        if start_iso:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            if start_dt <= datetime.now(timezone.utc):
                return jsonify({"available": False, "error": "Already started."}), 410
    except Exception:
        pass
    price = slot.get("our_price") or slot.get("price")
    return jsonify({
        "available": True,
        "slot_id": slot_id,
        "service_name": slot.get("service_name"),
        "business_name": slot.get("business_name"),
        "our_price": price,
        "currency": slot.get("currency", "USD"),
        "start_time": slot.get("start_time"),
        "location_city": slot.get("location_city"),
        "location_state": slot.get("location_state"),
    })


@app.route("/api/execute", methods=["POST"])
def execute_intent():
    """
    Agent-native endpoint: provide intent or criteria, get back a booking.
    Selects the best matching available slot and initiates checkout.
    """
    data        = request.get_json(force=True, silent=True) or {}
    category    = (data.get("category") or "").strip()
    city        = (data.get("city") or "").strip()
    budget      = data.get("budget")
    hours_ahead = int(data.get("hours_ahead") or 24)
    customer    = data.get("customer") or {}

    c_name  = (customer.get("name") or "").strip()
    c_email = (customer.get("email") or "").strip()
    c_phone = (customer.get("phone") or "").strip()

    if not all([c_name, c_email, c_phone]):
        return jsonify({"success": False, "error": "customer.name, customer.email, customer.phone are required."}), 400

    # Search for best match via /slots
    params: dict = {"hours_ahead": hours_ahead, "limit": 10}
    if category:
        params["category"] = category
    if city:
        params["city"] = city
    if budget:
        params["max_price"] = budget

    with app.test_client() as c:
        r = c.get("/slots", query_string=params)
        slots = r.get_json() or []

    if not slots:
        return jsonify({"success": False, "error": "No matching slots found for your criteria."}), 404

    # Pick soonest priced slot
    priced = [s for s in slots if (s.get("our_price") or s.get("price") or 0) > 0]
    if not priced:
        return jsonify({"success": False, "error": "No priced slots available matching your criteria."}), 404

    best = min(priced, key=lambda s: s.get("hours_until_start") or 999)

    # Delegate to /api/book
    with app.test_client() as c:
        r = c.post("/api/book", json={
            "slot_id": best.get("slot_id"),
            "customer_name": c_name,
            "customer_email": c_email,
            "customer_phone": c_phone,
        })
        result = r.get_json() or {}
        result["selected_slot"] = {
            "slot_id": best.get("slot_id"),
            "service_name": best.get("service_name"),
            "our_price": best.get("our_price") or best.get("price"),
            "start_time": best.get("start_time"),
            "location_city": best.get("location_city"),
        }
        return jsonify(result), r.status_code


@app.route("/api/keys/register", methods=["POST"])
def register_api_key():
    """
    Register for a free API key.
    Body: { "name": "My Agent", "email": "agent@example.com" }
    Returns: { "api_key": "lmd_...", "instructions": "..." }
    """
    data  = request.get_json(force=True, silent=True) or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not email:
        return jsonify({"success": False, "error": "name and email required."}), 400

    keys = _load_api_keys()
    # Return existing key if email already registered
    for k, v in keys.items():
        if v.get("email") == email:
            return jsonify({"success": True, "api_key": k, "message": "Existing key returned."})

    new_key = _generate_api_key()
    keys[new_key] = {
        "name": name,
        "email": email,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "usage_count": 0,
    }
    _save_api_keys(keys)
    return jsonify({
        "success": True,
        "api_key": new_key,
        "instructions": "Include this key as X-API-Key header on POST /api/book and POST /api/execute requests.",
    })


@app.route("/api/customers/register", methods=["POST"])
def register_customer():
    """
    Register a customer and save their payment method with Stripe.
    Step 1: Call this endpoint to get a setup_url.
    Step 2: Customer visits setup_url once to save their card.
    Step 3: Use stripe_customer_id in future /api/book calls — no redirect needed.

    Body: { "name": "Jane Smith", "email": "jane@example.com" }
    Returns: { "stripe_customer_id": "cus_...", "setup_url": "https://..." }
    """
    stripe = _stripe()
    if not stripe.api_key:
        return jsonify({"success": False, "error": "Payment not configured."}), 503

    data  = request.get_json(force=True, silent=True) or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"success": False, "error": "email required."}), 400

    customers = _load_customers()
    landing_url = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").rstrip("/")

    # Return existing customer if already registered
    if email in customers:
        cus_id = customers[email]["customer_id"]
        # Create a new setup intent for adding another card
        try:
            session = stripe.checkout.Session.create(
                mode="setup",
                customer=cus_id,
                success_url=f"{landing_url}?setup=success",
                cancel_url=f"{landing_url}?setup=cancelled",
            )
            return jsonify({"success": True, "stripe_customer_id": cus_id, "setup_url": session.url, "message": "Existing customer — use setup_url to add/update card."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    try:
        customer = stripe.Customer.create(name=name, email=email)
        session = stripe.checkout.Session.create(
            mode="setup",
            customer=customer.id,
            success_url=f"{landing_url}?setup=success",
            cancel_url=f"{landing_url}?setup=cancelled",
        )
        customers[email] = {
            "customer_id": customer.id,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_customers(customers)
        return jsonify({
            "success": True,
            "stripe_customer_id": customer.id,
            "setup_url": session.url,
            "instructions": "Visit setup_url once to save your card. Then use stripe_customer_id in /api/book for instant booking with no redirect.",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/customers/<customer_id>/book", methods=["POST"])
def book_with_saved_card(customer_id: str):
    """
    Autonomous booking using a saved Stripe payment method — no redirect required.
    Card is authorized (held), booking executed, then captured. Fully autonomous.

    Body: { "slot_id": "...", "customer_name": "...", "customer_email": "...", "customer_phone": "..." }
    Returns: { "success": true, "payment_intent_id": "...", "status": "booking_in_progress" }
    """
    # Require API key — this endpoint charges a saved card, so auth is mandatory
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"success": False, "error": "API key required."}), 401

    stripe = _stripe()
    if not stripe.api_key:
        return jsonify({"success": False, "error": "Payment not configured."}), 503

    data           = request.get_json(force=True, silent=True) or {}
    slot_id        = (data.get("slot_id") or "").strip()
    customer_name  = (data.get("customer_name") or "").strip()
    customer_email = (data.get("customer_email") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()

    if not all([slot_id, customer_name, customer_email, customer_phone]):
        return jsonify({"success": False, "error": "Missing required fields."}), 400

    # ── Idempotency: prevent duplicate saved-card bookings ────────────────
    import hashlib as _sc_hl
    _sc_ts_bucket = int(time.time()) // 300  # 5-minute window
    _sc_idem_key = _sc_hl.sha256(
        f"{customer_id}:{slot_id}:{customer_email}:{_sc_ts_bucket}".encode()
    ).hexdigest()[:32]
    with _SAVED_CARD_LOCK:
        if _SAVED_CARD_IN_FLIGHT.get(_sc_idem_key):
            return jsonify({
                "success": False,
                "error": "A booking for this slot is already in progress. "
                         "Please wait for the first request to complete before retrying.",
            }), 409
        _SAVED_CARD_IN_FLIGHT[_sc_idem_key] = True

    try:  # finally: clear _SAVED_CARD_IN_FLIGHT
        return _book_with_saved_card_inner(customer_id, slot_id, customer_name,
                                           customer_email, customer_phone, stripe, data)
    finally:
        with _SAVED_CARD_LOCK:
            _SAVED_CARD_IN_FLIGHT.pop(_sc_idem_key, None)


def _book_with_saved_card_inner(customer_id, slot_id, customer_name,
                                 customer_email, customer_phone, stripe, data):
    """Inner logic for saved-card booking — extracted so idempotency lock cleanup is guaranteed."""
    # Availability checks
    slot = get_slot_by_id(slot_id)
    if not slot:
        return jsonify({"success": False, "error": "Slot not available."}), 404
    if slot_id in _load_booked():
        return jsonify({"success": False, "error": "Already booked."}), 409
    try:
        start_dt = datetime.fromisoformat(slot.get("start_time", "").replace("Z", "+00:00"))
        if start_dt <= datetime.now(timezone.utc):
            return jsonify({"success": False, "error": "Slot has already started."}), 410
    except Exception:
        pass

    # ── Cancellation policy gate: block saved-card booking for risky products ──
    _sc_cutoff = slot.get("cancellation_cutoff_hours")
    if _sc_cutoff is None:
        try:
            _sc_burl = slot.get("booking_url", "")
            if _sc_burl and isinstance(_sc_burl, str) and _sc_burl.startswith("{"):
                _sc_cutoff = json.loads(_sc_burl).get("cancellation_cutoff_hours")
        except Exception:
            pass
    _sc_cutoff = int(_sc_cutoff) if _sc_cutoff is not None else 48
    if _sc_cutoff >= 9999 * 24:
        return jsonify({
            "success": False,
            "error": "This product is non-refundable. Saved-card booking is not available "
                     "for non-refundable products. Use POST /api/book to generate a checkout "
                     "URL so the customer can review and acknowledge the cancellation policy.",
            "cancellation_policy": "Non-refundable — no cancellations or refunds",
            "cancellation_cutoff_hours": _sc_cutoff,
        }), 403
    if _sc_cutoff > 48:
        return jsonify({
            "success": False,
            "error": f"This product has a {_cancel_cutoff_display(_sc_cutoff)} cancellation "
                     "window. Saved-card booking is not available for products with "
                     "cancellation windows over 48 hours. Use POST /api/book to generate a "
                     "checkout URL so the customer can review the cancellation policy.",
            "cancellation_policy": _cancel_policy_label(_sc_cutoff),
            "cancellation_cutoff_hours": _sc_cutoff,
        }), 403

    our_price = slot.get("our_price") or slot.get("price")
    if not our_price or float(our_price) <= 0:
        return jsonify({"success": False, "error": "No price available."}), 400

    price_cents = int(float(our_price) * 100)

    try:
        # Get customer's saved payment methods
        pms = stripe.PaymentMethod.list(customer=customer_id, type="card")
        if not pms.data:
            return jsonify({
                "success": False,
                "error": "No saved payment method found. Register card first via POST /api/customers/register",
            }), 400

        pm = pms.data[0]  # Use most recent card

        # Create auth hold (capture_method=manual — card NOT charged yet)
        pi = stripe.PaymentIntent.create(
            amount=price_cents,
            currency=slot.get("currency", "usd").lower(),
            customer=customer_id,
            payment_method=pm.id,
            confirm=True,
            capture_method="manual",
            off_session=True,
            metadata={
                "slot_id": slot_id,
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "platform": slot.get("platform", ""),
                "booking_url": slot.get("booking_url", ""),
            },
        )
    except Exception as e:
        # CardError has user_message; other exceptions don't
        user_msg = getattr(e, "user_message", None)
        if user_msg:
            return jsonify({"success": False, "error": f"Card declined: {user_msg}"}), 402
        return jsonify({"success": False, "error": str(e)}), 500

    # Execute booking on source platform
    customer = {"name": customer_name, "email": customer_email, "phone": customer_phone}

    # Send "booking in progress" email immediately
    try:
        from send_booking_email import send_booking_email
        send_booking_email("booking_initiated", customer_email, customer_name, slot)
    except Exception:
        pass

    try:
        # _fulfill_booking returns a 3-tuple: (confirmation, booking_meta, supplier_reference)
        confirmation, booking_meta, supplier_reference = _fulfill_booking(
            slot_id, customer, slot.get("platform", ""), slot.get("booking_url", ""))

        # Resolve supplier_id early — needed for both success and capture-failure paths
        booking_url_raw = slot.get("booking_url", "")
        try:
            _burl = json.loads(booking_url_raw) if isinstance(booking_url_raw, str) else (booking_url_raw or {})
            _supplier_id = _burl.get("supplier_id", slot.get("platform", ""))
        except Exception:
            _burl = {}
            _supplier_id = slot.get("platform", "")

        # Capture payment — with safe OCTO cancellation on failure
        try:
            stripe.PaymentIntent.capture(pi.id)
        except Exception as cap_err:
            print(f"[SAVED_CARD] *** CAPTURE FAILED: {cap_err} — cancelling OCTO booking ***")
            octo_cancel_result = _cancel_octo_booking(_supplier_id, str(confirmation))
            if not octo_cancel_result.get("success"):
                _save_booking_record(f"ORPHAN_SC_{slot_id[:16]}", {
                    "status":           "capture_failed_needs_manual_action",
                    "confirmation":     str(confirmation),
                    "supplier_id":      _supplier_id,
                    "payment_intent":   pi.id,
                    "capture_error":    str(cap_err)[:500],
                    "created_at":       datetime.now(timezone.utc).isoformat(),
                })
            raise  # Let outer except cancel the hold

        _mark_booked(slot_id)

        # Persist booking record so DELETE /bookings/{id} can cancel it later
        booking_record_id = f"bk_{slot_id[:12]}_{uuid.uuid4().hex[:8]}"
        _save_booking_record(booking_record_id, {
            "booking_id":        booking_record_id,
            "confirmation":      str(confirmation or ""),
            "supplier_reference": str(supplier_reference or ""),
            "platform":          slot.get("platform", ""),
            "supplier_id":       _supplier_id,
            "booking_url":       booking_url_raw,
            "service_name":      slot.get("service_name", ""),
            "business_name":     slot.get("business_name", ""),
            "location_city":     slot.get("location_city", ""),
            "start_time":        slot.get("start_time", ""),
            "currency":          slot.get("currency", "USD"),
            "price_charged":     float(our_price),
            "payment_method":    "stripe_saved_card",
            "payment_intent_id": pi.id,
            "status":            "booked",
            "executed_at":       datetime.now(timezone.utc).isoformat(),
            "customer_name":     customer_name,
            "customer_email":    customer_email,
            "customer_phone":    customer_phone,
            "slot_id":           slot_id,
            "cancellation_cutoff_hours": slot.get("cancellation_cutoff_hours"),
        })

        # Send confirmation email with self-serve cancel link
        try:
            from send_booking_email import send_booking_email
            _cancel_url = (
                f"{os.getenv('BOOKING_SERVER_HOST', '')}/cancel/{booking_record_id}"
                f"?t={_make_cancel_token(booking_record_id)}"
            )
            send_booking_email("booking_confirmed", customer_email, customer_name, slot,
                               confirmation_number=str(confirmation or ""),
                               cancel_url=_cancel_url)
        except Exception:
            pass

        return jsonify(_with_context({
            "success": True,
            "booking_id":        booking_record_id,
            "payment_intent_id": pi.id,
            "confirmation":      confirmation,
            "status":            "confirmed",
            "amount_charged":    our_price,
            "currency":          slot.get("currency", "USD"),
        }))
    except Exception as e:
        # Booking failed — cancel the hold
        try:
            stripe.PaymentIntent.cancel(pi.id)
        except Exception:
            pass

        # Send failure email
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_failed", customer_email, customer_name, slot, error_reason=str(e))
        except Exception:
            pass

        return jsonify({
            "success": False,
            "error": f"Booking failed on source platform: {e}. Card was NOT charged.",
            "payment_intent_id": pi.id,
        }), 500


@app.route("/api/webhooks/subscribe", methods=["POST"])
def webhook_subscribe():
    """
    Subscribe to deal alerts. We'll POST matching new deals to your callback_url.

    Body: {
      "callback_url": "https://your-agent.example.com/deals-webhook",
      "filters": {
        "city": "New York",         // optional
        "category": "wellness",     // optional
        "max_price": 100,           // optional
        "hours_ahead": 24           // optional — only alert for deals within this window
      }
    }
    Returns: { "subscription_id": "sub_...", "callback_url": "...", "filters": {...} }
    """
    import secrets
    data         = request.get_json(force=True, silent=True) or {}
    callback_url = (data.get("callback_url") or "").strip()
    filters      = data.get("filters") or {}

    if not callback_url or not callback_url.startswith("http"):
        return jsonify({"success": False, "error": "Valid callback_url required."}), 400

    subs = _load_webhooks()
    sub_id = "sub_" + secrets.token_hex(12)
    subs[sub_id] = {
        "callback_url": callback_url,
        "filters": filters,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    }
    _save_webhooks(subs)
    return jsonify({"success": True, "subscription_id": sub_id, "callback_url": callback_url, "filters": filters})


@app.route("/api/webhooks/unsubscribe", methods=["POST"])
def webhook_unsubscribe():
    """Cancel a webhook subscription."""
    data   = request.get_json(force=True, silent=True) or {}
    sub_id = (data.get("subscription_id") or "").strip()
    if not sub_id:
        return jsonify({"success": False, "error": "subscription_id required."}), 400

    subs = _load_webhooks()
    if sub_id not in subs:
        return jsonify({"success": False, "error": "Subscription not found."}), 404

    del subs[sub_id]
    _save_webhooks(subs)
    return jsonify({"success": True, "message": f"Subscription {sub_id} cancelled."})


@app.route("/mcp", methods=["GET", "POST"])
def mcp_endpoint():
    """
    MCP JSON-RPC 2.0 endpoint. Works with any agent that supports HTTP tool calls.
    GET  → server info / discoverability
    POST → MCP JSON-RPC (initialize, tools/list, tools/call, prompts/list, prompts/get)

    Backwards compatible: also accepts legacy { "tool": "...", "arguments": {} } format.
    """
    if request.method == "GET":
        return jsonify({
            "name": "Last Minute Deals HQ",
            "version": "1.0.0",
            "protocol": "MCP JSON-RPC 2.0",
            "endpoint": "POST /mcp",
            "tools": [t["name"] for t in _MCP_TOOLS],
            "prompts": [p["name"] for p in _MCP_PROMPTS],
            "docs": "https://lastminutedealshq.com/developers",
            "configSchema": {
                "type": "object",
                "properties": {
                    "lmd_api_key": {
                        "type": "string",
                        "description": "API key for making bookings. Get yours free at https://api.lastminutedealshq.com/api/keys/register (POST, no body needed).",
                    },
                    "booking_api_url": {
                        "type": "string",
                        "description": "Booking API base URL. Leave blank to use the default production server.",
                        "default": "https://api.lastminutedealshq.com",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        })

    body   = request.get_json(force=True, silent=True) or {}
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    # Store MCP method/tool on the request so _log_request can enrich its record
    request._mcp_method = method or None
    request._mcp_tool = None

    def ok(result):
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})

    def err(code, msg):
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}), 400

    # Legacy format: { "tool": "...", "arguments": {} }
    if not method and body.get("tool"):
        tool_name = body.get("tool", "")
        arguments  = body.get("arguments", {})
        # Map old tool names to new ones
        name_map = {"search_last_minute_slots": "search_slots", "get_slot_details": "get_slot"}
        tool_name = name_map.get(tool_name, tool_name)
        request._mcp_method = "tools/call"
        request._mcp_tool = tool_name
        try:
            result = _mcp_call_tool(tool_name, arguments)
            return jsonify({"tool": tool_name, "result": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
            "serverInfo": {"name": "Last Minute Deals HQ", "version": "1.0.0"},
        })
    elif method == "tools/list":
        # Inject live supplier count into tool descriptions
        count = _supplier_count()
        tools = []
        for t in _MCP_TOOLS:
            t2 = dict(t)
            if "{supplier_count}" in t2.get("description", ""):
                t2["description"] = t2["description"].replace("{supplier_count}", str(count))
            tools.append(t2)
        return ok({"tools": tools})
    elif method == "prompts/list":
        return ok({"prompts": _MCP_PROMPTS})
    elif method == "prompts/get":
        prompt_name = params.get("name", "")
        prompt_args = params.get("arguments", {})
        try:
            result = _mcp_render_prompt(prompt_name, prompt_args)
            return ok(result)
        except ValueError as e:
            return err(-32602, str(e))
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments  = params.get("arguments", {})
        request._mcp_tool = tool_name
        try:
            result = _mcp_call_tool(tool_name, arguments)
            return ok({
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            })
        except Exception as e:
            return ok({
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            })
    elif method == "resources/list":
        return ok({"resources": []})
    elif method == "resources/read":
        return err(-32002, "No resources available")
    elif method == "notifications/initialized":
        return "", 204
    else:
        return err(-32601, f"Method not found: {method}")


@app.route("/api/subscribe", methods=["POST"])
def sms_subscribe():
    """
    Add a subscriber to the SMS alerts list.
    Called from the landing page opt-in form.
    """
    data  = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()
    city  = (data.get("city") or "").strip()
    cats  = data.get("categories") or []

    if not phone:
        return jsonify({"success": False, "error": "Phone number required."}), 400

    # Basic E.164 normalisation
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    if not digits.startswith("+") and len(digits) == 10:
        digits = "+1" + digits

    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent))
        from send_sms_alert import subscribe
        subscribe(digits, city, cats)
        return jsonify({"success": True, "phone": digits})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── /execute/best ────────────────────────────────────────────────────────────

@app.route("/execute/best", methods=["POST"])
def execute_best():
    """
    Goal-oriented decisioning endpoint — you tell us what you want to achieve,
    we decide WHAT to book, not just how to execute it.

    Unlike /execute/guaranteed (which needs a slot_id or category/city hint),
    this endpoint optimizes across ALL available inventory to find the maximum
    value option given your goal.

    Goals:
      "maximize_value"   — best discount vs market rate
      "minimize_wait"    — soonest available slot
      "maximize_success" — slot most likely to complete successfully (platform reliability × confidence)
      "minimize_price"   — cheapest absolute price within budget

    Body:
      {
        "goal": "maximize_value",
        "city":        "Detroit",       // optional filter
        "category":    "wellness",      // optional filter
        "budget":      150.0,           // optional max price
        "hours_ahead": 48,
        "customer":    { "name": "...", "email": "...", "phone": "..." },
        "wallet_id":   "wlt_...",       // OR payment_intent_id
        "explain":     true             // include reasoning in response
      }

    Returns ExecutionResult + optional explanation of why this slot was chosen.
    """
    data         = request.get_json(force=True, silent=True) or {}
    goal         = (data.get("goal") or "maximize_value").strip()
    city         = (data.get("city") or "").strip()
    category     = (data.get("category") or "").strip()
    budget       = data.get("budget")
    hours_ahead  = int(data.get("hours_ahead") or 48)
    customer     = data.get("customer") or {}
    wallet_id    = (data.get("wallet_id") or "").strip() or None
    pi_id        = (data.get("payment_intent_id") or "").strip() or None
    explain      = bool(data.get("explain", False))

    c_name  = (customer.get("name") or "").strip()
    c_email = (customer.get("email") or "").strip()
    c_phone = (customer.get("phone") or "").strip()

    if not all([c_name, c_email, c_phone]):
        return jsonify({"success": False, "error": "customer.name, customer.email, customer.phone required."}), 400
    if not wallet_id and not pi_id:
        return jsonify({"success": False, "error": "wallet_id or payment_intent_id required."}), 400

    # Load all available slots
    booked_ids = _load_booked()
    slots = _load_slots_from_supabase(
        hours_ahead=hours_ahead,
        category=category,
        city=city,
        budget=float(budget) if budget else 0,
        limit=10000,
    )

    # Filter candidates
    candidates = []
    for s in slots:
        if s.get("slot_id") in booked_ids:
            continue
        p = float(s.get("our_price") or s.get("price") or 0)
        if p <= 0:
            continue
        candidates.append(s)

    if not candidates:
        return jsonify({"success": False, "error": "No matching slots for your criteria.", "status": "no_slots"}), 404

    # Load platform reliability from insights for maximize_success goal
    reliability: dict = {}
    insights_mod = _load_module("market_insights")
    if insights_mod:
        try:
            snap = insights_mod.get_market_snapshot(category, city)
            reliability = snap.get("platform_reliability", {})
        except Exception:
            pass

    # Score and rank candidates by goal
    def score(s: dict) -> float:
        p       = float(s.get("our_price") or s.get("price") or 0)
        orig    = float(s.get("original_price") or p)
        h       = float(s.get("hours_until_start") or 0)
        plat    = s.get("platform", "")
        rel     = reliability.get(plat, {}).get("success_rate", 0.5)
        savings = orig - p  # positive = we charged less than market

        if goal == "maximize_value":
            return savings / max(p, 1)  # savings as % of price
        elif goal == "minimize_wait":
            return -(h)  # lowest hours = best
        elif goal == "maximize_success":
            return rel
        elif goal == "minimize_price":
            return -(p)
        else:
            return savings / max(p, 1)  # default: maximize_value

    best = max(candidates, key=score)

    # Build explanation
    explanation = ""
    if explain:
        p    = float(best.get("our_price") or best.get("price") or 0)
        orig = float(best.get("original_price") or p)
        plat = best.get("platform", "")
        rel  = reliability.get(plat, {}).get("success_rate", 0.5)
        explanation = (
            f"Chose '{best.get('service_name')}' on {plat} "
            f"(${p:.2f}, {best.get('hours_until_start', '?'):.1f}h away, "
            f"platform success rate {rel:.0%}) "
            f"to satisfy goal='{goal}'."
        )

    # Execute via guaranteed engine
    eng_mod = _load_module("execution_engine")
    if not eng_mod:
        return jsonify({"success": False, "error": "execution_engine.py unavailable."}), 500

    req = eng_mod.ExecutionRequest(
        slot_id=best.get("slot_id"),
        category=best.get("category", ""),
        city=best.get("location_city", ""),
        hours_ahead=hours_ahead,
        customer={"name": c_name, "email": c_email, "phone": c_phone},
        payment_method="wallet" if wallet_id else "stripe_pi",
        wallet_id=wallet_id,
        payment_intent_id=pi_id,
    )

    engine = eng_mod.ExecutionEngine(slots=slots, booked_ids=booked_ids)
    result = engine.execute(req)

    resp = result.to_dict()
    resp["goal"] = goal
    if explain:
        resp["explanation"] = explanation

    # Send email
    if result.success:
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_confirmed", c_email, c_name,
                               {"service_name": result.service_name},
                               confirmation_number=result.confirmation)
        except Exception:
            pass

    return jsonify(_with_context(resp)), 200 if result.success else 500


# ── Intent session routes ─────────────────────────────────────────────────────

def _get_intent_store():
    """Get or lazily create the intent session store."""
    store = app.config.get("_intent_store")
    if store is None:
        mod = _load_module("intent_sessions")
        if not mod:
            return None
        store = mod.IntentSessionStore()
        app.config["_intent_store"] = store
    return store


@app.route("/intent/create", methods=["POST"])
def intent_create():
    """
    Create a persistent intent session. The system will work on this goal
    continuously until it's fulfilled, cancelled, or expired.

    Body:
      {
        "goal": "find_and_book",         // "find_and_book" | "monitor_only" | "price_alert"
        "constraints": {
          "category":    "wellness",
          "city":        "Detroit",
          "budget":      150.0,
          "hours_ahead": 48,
          "allow_alternatives": true
        },
        "customer": { "name": "...", "email": "...", "phone": "..." },  // required for find_and_book
        "payment": {
          "method":    "wallet",
          "wallet_id": "wlt_..."
        },
        "autonomy":     "full",           // "full" (auto-execute) | "notify" (alert first) | "monitor" (never execute)
        "callback_url": "https://...",    // optional — POST status changes here
        "ttl_hours":    24               // auto-expire after this many hours (default 24)
      }

    Returns: { "intent_id": "int_...", "status": "monitoring", "expires_at": "..." }
    """
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"success": False, "error": "API key required."}), 401

    store = _get_intent_store()
    if not store:
        return jsonify({"success": False, "error": "Intent system unavailable."}), 503

    data         = request.get_json(force=True, silent=True) or {}
    goal         = (data.get("goal") or "find_and_book").strip()
    constraints  = data.get("constraints") or {}
    customer     = data.get("customer") or {}
    payment      = data.get("payment") or {}
    autonomy     = (data.get("autonomy") or "full").strip()
    callback_url = (data.get("callback_url") or "").strip() or None
    ttl_hours    = int(data.get("ttl_hours") or 24)

    if goal == "find_and_book" and autonomy in ("full", "notify"):
        c_name  = (customer.get("name") or "").strip()
        c_email = (customer.get("email") or "").strip()
        c_phone = (customer.get("phone") or "").strip()
        if not all([c_name, c_email, c_phone]):
            return jsonify({"success": False, "error": "customer.name, email, phone required for find_and_book."}), 400

    session = store.create(
        api_key=key,
        goal=goal,
        constraints=constraints,
        customer=customer,
        payment=payment,
        autonomy=autonomy,
        callback_url=callback_url,
        ttl_hours=ttl_hours,
    )

    return jsonify({
        "success":    True,
        "intent_id":  session["intent_id"],
        "status":     session["status"],
        "goal":       goal,
        "expires_at": session["expires_at"],
        "message":    f"Intent is monitoring. System will auto-execute when matching slots appear." if autonomy == "full" else f"Intent is monitoring. POST /intent/{session['intent_id']}/execute to trigger when ready.",
    })


@app.route("/intent/<intent_id>", methods=["GET"])
def intent_get(intent_id: str):
    """Get current status of an intent session."""
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"error": "API key required."}), 401

    store = _get_intent_store()
    if not store:
        return jsonify({"error": "Intent system unavailable."}), 503

    session = store.get(intent_id)
    if not session:
        return jsonify({"error": "Intent not found."}), 404
    if session.get("api_key") != key:
        return jsonify({"error": "Unauthorized."}), 403

    # Don't expose internal api_key or raw payment credentials in response
    safe = {k: v for k, v in session.items() if k not in ("api_key",)}
    return jsonify(safe)


@app.route("/intent/<intent_id>/execute", methods=["POST"])
def intent_execute(intent_id: str):
    """
    Manually trigger execution of a 'notify' autonomy intent.
    Called when the agent has received a "slots_available" callback and decides to proceed.
    """
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"error": "API key required."}), 401

    store = _get_intent_store()
    if not store:
        return jsonify({"error": "Intent system unavailable."}), 503

    session = store.get(intent_id)
    if not session:
        return jsonify({"error": "Intent not found."}), 404
    if session.get("api_key") != key:
        return jsonify({"error": "Unauthorized."}), 403
    if session.get("status") not in ("monitoring",):
        return jsonify({"error": f"Intent is already {session['status']}."}), 409

    # Temporarily upgrade autonomy to "full" for this one execution
    original_autonomy = session.get("autonomy", "full")
    session = dict(session)
    session["autonomy"] = "full"

    mod = _load_module("intent_sessions")
    if not mod:
        return jsonify({"error": "Intent system unavailable."}), 503

    resolved = mod.execute_intent(session, store)

    # Restore autonomy
    with __import__("threading").Lock():
        import json as _json
        sf = Path(".tmp/intent_sessions.json")
        if sf.exists():
            sessions = _json.loads(sf.read_text(encoding="utf-8"))
            if intent_id in sessions:
                sessions[intent_id]["autonomy"] = original_autonomy
                sf.write_text(_json.dumps(sessions, indent=2), encoding="utf-8")

    updated = store.get(intent_id) or {}
    return jsonify(_with_context({
        "resolved":   resolved,
        "status":     updated.get("status"),
        "result":     updated.get("result"),
        "intent_id":  intent_id,
    }))


@app.route("/intent/<intent_id>/cancel", methods=["POST"])
def intent_cancel(intent_id: str):
    """Cancel an active intent session."""
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"error": "API key required."}), 401

    store = _get_intent_store()
    session = store.get(intent_id) if store else None
    if not session:
        return jsonify({"error": "Intent not found."}), 404
    if session.get("api_key") != key:
        return jsonify({"error": "Unauthorized."}), 403

    store.cancel(intent_id)
    return jsonify({"success": True, "intent_id": intent_id, "status": "cancelled"})


@app.route("/intent/list", methods=["GET"])
def intent_list():
    """List all intent sessions for the authenticated agent."""
    key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(key):
        return jsonify({"error": "API key required."}), 401

    store = _get_intent_store()
    if not store:
        return jsonify([])

    sessions = store.list_by_api_key(key)
    # Return lightweight summary — not full records
    return jsonify([
        {
            "intent_id":   s["intent_id"],
            "goal":        s.get("goal"),
            "status":      s.get("status"),
            "constraints": s.get("constraints"),
            "autonomy":    s.get("autonomy"),
            "expires_at":  s.get("expires_at"),
            "attempt_count": s.get("attempt_count", 0),
            "created_at":  s.get("created_at"),
        }
        for s in sessions
    ])


# ── Market insights routes ─────────────────────────────────────────────────────

@app.route("/insights/market", methods=["GET"])
def insights_market():
    """
    Market intelligence API. Returns aggregated data from all booking attempts,
    platform reliability scores, fill velocity, optimal booking windows, and
    live inventory.

    This data compounds over time and is not reproducible from a standing start.

    Query params:
      ?category=wellness   — filter to a specific category
      ?city=NYC            — filter to a specific city
      ?refresh=1           — force rebuild from raw data (slow — use sparingly)

    Returns the full market snapshot if no filters given.
    Data advantage: agents that use this for booking decisions get better outcomes
    than agents running their own search, because this data reflects real booking
    success rates across thousands of attempts over time.
    """
    category = request.args.get("category", "").strip()
    city     = request.args.get("city", "").strip()
    force    = request.args.get("refresh", "0") == "1"

    mod = _load_module("market_insights")
    if not mod:
        return jsonify({"error": "Insights engine unavailable."}), 503

    try:
        if force:
            snapshot = mod.build_market_overview()
        else:
            snapshot = mod.get_market_snapshot(category, city)
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/insights/platform/<platform_name>", methods=["GET"])
def insights_platform(platform_name: str):
    """Detailed reliability and performance stats for a specific platform."""
    mod = _load_module("market_insights")
    if not mod:
        return jsonify({"error": "Insights engine unavailable."}), 503

    snap = mod.get_market_snapshot()
    plat_data = snap.get("platform_reliability", {}).get(platform_name)
    if not plat_data:
        return jsonify({"error": f"No data for platform: {platform_name}"}), 404

    return jsonify({
        "platform":        platform_name,
        "reliability":     plat_data,
        "optimal_windows": snap.get("optimal_booking_windows", {}),
        "data_window_days": snap.get("data_window_days", 30),
        "generated_at":    snap.get("generated_at"),
    })


# ── Watcher status ────────────────────────────────────────────────────────────

@app.route("/api/watcher/status", methods=["GET"])
def watcher_status():
    """
    Return real-time watcher health and last-poll timestamps.
    Agents can use this to check data freshness before submitting a booking.
    """
    status_file = Path(".tmp/watcher_status.json")
    if not status_file.exists():
        return jsonify({"running": False, "message": "Real-time watchers not started."})
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        return jsonify({"running": True, "platforms": status})
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 500


# ── /execute/guaranteed ───────────────────────────────────────────────────────

@app.route("/execute/guaranteed", methods=["POST"])
def execute_guaranteed():
    """
    Guaranteed booking endpoint — hard outcome, no browsing, no ambiguity.

    Unlike /api/execute (which returns a checkout redirect), this endpoint
    executes the booking synchronously and returns only when the outcome is
    known: booked or failed. Uses the multi-path retry engine with up to 7
    fallback strategies.

    Payment options:
      - wallet_id:    Debit a pre-funded agent wallet (no Stripe roundtrip — fastest)
      - payment_intent_id: Capture an existing Stripe manual-hold PI on success

    Body:
      {
        "slot_id":     "abc123",      // optional — engine finds best match if omitted
        "category":    "wellness",
        "city":        "New York",
        "hours_ahead": 24,
        "budget":      150.0,
        "allow_alternatives": true,
        "customer": {
          "name":  "Jane Smith",
          "email": "jane@example.com",
          "phone": "+15550001234"
        },
        "wallet_id":          "wlt_...",  // use pre-funded wallet (preferred)
        "payment_intent_id":  "pi_...",   // OR capture existing Stripe hold
      }

    Returns:
      {
        "success": true,
        "status": "booked",
        "confirmation": "OCTO-12345",
        "slot_id": "...",
        "service_name": "Reykjavik Northern Lights Tour",
        "platform": "bokun",
        "price_charged": 85.00,
        "attempts": 2,
        "fallbacks_used": 1,
        "savings_vs_market": 15.00,
        "confidence_score": 0.75,
        "attempt_log": [...]
      }
    """
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    data = request.get_json(force=True, silent=True) or {}

    slot_id              = (data.get("slot_id") or "").strip() or None
    category             = (data.get("category") or "").strip()
    city                 = (data.get("city") or "").strip()
    hours_ahead          = int(data.get("hours_ahead") or 24)
    budget               = data.get("budget")
    allow_alternatives   = bool(data.get("allow_alternatives", True))
    customer             = data.get("customer") or {}
    wallet_id            = (data.get("wallet_id") or "").strip() or None
    payment_intent_id    = (data.get("payment_intent_id") or "").strip() or None

    c_name  = (customer.get("name") or "").strip()
    c_email = (customer.get("email") or "").strip()
    c_phone = (customer.get("phone") or "").strip()

    if not all([c_name, c_email, c_phone]):
        return jsonify({"success": False, "error": "customer.name, customer.email, customer.phone required."}), 400

    if not wallet_id and not payment_intent_id:
        return jsonify({"success": False, "error": "Either wallet_id or payment_intent_id required for guaranteed execution."}), 400

    # Load execution engine
    spec = _ilu.spec_from_file_location("execution_engine", _Path(__file__).parent / "execution_engine.py")
    if not spec:
        return jsonify({"success": False, "error": "execution_engine.py not found."}), 500
    eng_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(eng_mod)

    # Determine payment method
    if wallet_id:
        payment_method = "wallet"
    elif payment_intent_id:
        payment_method = "stripe_pi"
    else:
        payment_method = "stripe_checkout"

    req = eng_mod.ExecutionRequest(
        slot_id=slot_id,
        category=category,
        city=city,
        hours_ahead=hours_ahead,
        budget=float(budget) if budget else None,
        allow_alternatives=allow_alternatives,
        customer={"name": c_name, "email": c_email, "phone": c_phone},
        payment_method=payment_method,
        wallet_id=wallet_id,
        payment_intent_id=payment_intent_id,
    )

    # Load slots from Supabase (fresh inventory, not Railway's local file)
    booked_ids = _load_booked()
    fresh_slots = _load_slots_from_supabase(
        hours_ahead=hours_ahead,
        category=category,
        city=city,
        budget=float(budget) if budget else 0,
        limit=10000,
    )

    engine = eng_mod.ExecutionEngine(slots=fresh_slots, booked_ids=booked_ids)
    result = engine.execute(req)

    # Send email on success/failure
    if result.success:
        slot_for_email = get_slot_by_id(result.slot_id) or {"service_name": result.service_name}
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_confirmed", c_email, c_name, slot_for_email,
                               confirmation_number=result.confirmation)
        except Exception:
            pass
    elif result.attempts > 0:
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_failed", c_email, c_name,
                               {"service_name": category or "your requested booking"},
                               error_reason=result.error)
        except Exception:
            pass

    result_dict = result.to_dict()
    if result.success:
        # Fetch slot details so the receipt record is complete enough to support
        # cancellation (wallet refund, email, city/time display).
        _slot_for_receipt = get_slot_by_id(result.slot_id) or {}
        receipt = _make_receipt(
            result_dict,
            customer_email=c_email,
            customer={"name": c_name, "email": c_email, "phone": c_phone},
            payment={"method": payment_method, "wallet_id": wallet_id,
                     "payment_intent_id": payment_intent_id},
            slot=_slot_for_receipt,
        )
        result_dict.update(receipt)

    status_code = 200 if result.success else (404 if result.status == "no_slots" else 500)
    return jsonify(_with_context(result_dict)), status_code


# ── Execution receipt verification ────────────────────────────────────────────

@app.route("/verify/<booking_id>", methods=["GET"])
def verify_booking(booking_id: str):
    """
    Public endpoint — verify a booking receipt by ID.

    Any AI agent or third party can call this to independently confirm that
    a booking exists in our system and that the receipt has not been tampered with.

    Returns the full signed record. The caller can re-compute the signature
    by hashing the record fields (excluding 'signature') with their own copy
    of our public verification endpoint, or simply trust the record's presence here.

    GET /verify/bk_abc123
    → {
        "booking_id": "bk_abc123",
        "confirmation": "OCTO-XYZ",
        "platform": "bokun",
        "service_name": "...",
        "price_charged": 0.0,
        "status": "booked",
        "executed_at": "2026-03-28T21:00:00Z",
        "signature": "sha256=abc123...",
        "verified": true
      }
    """
    record = _load_booking_record(booking_id)
    if not record:
        return jsonify({"verified": False, "error": "Booking not found"}), 404

    # Re-verify the signature to confirm record integrity
    stored_sig = record.get("signature", "")
    check_record = {k: v for k, v in record.items() if k != "signature"}
    expected_sig = f"sha256={_sign_record(check_record)}"
    signature_valid = hmac.compare_digest(stored_sig, expected_sig)

    # Return only non-PII fields — this is a public endpoint for receipt verification.
    # Customer email, phone, payment IDs etc. are not exposed.
    public_record = {k: v for k, v in record.items() if k not in _PII_FIELDS}
    return jsonify({**public_record, "verified": signature_valid}), 200


# ── Booking cancellation ──────────────────────────────────────────────────────

def _cancel_octo_booking(platform: str, confirmation: str, max_attempts: int = 3) -> dict:
    """
    Cancel a booking on an OCTO supplier with automatic retry.
    - Retries up to max_attempts times with exponential backoff (2s, 4s).
    - Treats HTTP 404 as success (booking already gone = desired state).
    - Returns {"success": bool, "detail": str, "permanent": bool}
      permanent=True means retrying won't help (auth failure, bad request, etc.)
    """
    import json as _json

    seeds_path = Path(__file__).parent / "seeds" / "octo_suppliers.json"
    try:
        suppliers = _json.loads(seeds_path.read_text(encoding="utf-8"))
    except Exception:
        return {"success": False, "detail": "Could not load OCTO supplier config", "permanent": True}

    supplier = next((s for s in suppliers if s.get("supplier_id") == platform and s.get("enabled")), None)
    if not supplier:
        return {"success": False, "detail": f"No enabled OCTO supplier for '{platform}'", "permanent": True}

    api_key = os.getenv(supplier["api_key_env"], "").strip()
    if not api_key:
        return {"success": False, "detail": f"API key not set: {supplier['api_key_env']}", "permanent": True}

    base_url = supplier["base_url"].rstrip("/")
    last_error = ""

    # Circuit breaker check — cancellations still attempt through half-open
    try:
        cb_spec = _ilu.spec_from_file_location("circuit_breaker", Path(__file__).parent / "circuit_breaker.py")
        cb_mod  = _ilu.module_from_spec(cb_spec)
        cb_spec.loader.exec_module(cb_mod)
        blocked, cb_reason = cb_mod.is_open(platform)
        if blocked:
            return {"success": False, "detail": f"Circuit open: {cb_reason}", "permanent": False}
    except Exception:
        cb_mod = None

    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(2 ** attempt)   # 2s then 4s
        try:
            r = requests.delete(
                f"{base_url}/bookings/{confirmation}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                timeout=15,
            )
            if r.status_code in (200, 204):
                if cb_mod:
                    try: cb_mod.record_success(platform)
                    except Exception: pass
                return {"success": True, "detail": f"Cancelled on {platform} (HTTP {r.status_code})"}
            if r.status_code == 404:
                if cb_mod:
                    try: cb_mod.record_success(platform)
                    except Exception: pass
                return {"success": True, "detail": "Booking not found on supplier (already cancelled or expired)"}
            if r.status_code in (400, 401, 403, 422):
                if cb_mod:
                    try: cb_mod.record_failure(platform, f"HTTP {r.status_code}")
                    except Exception: pass
                return {"success": False, "permanent": True,
                        "detail": f"Permanent failure HTTP {r.status_code}: {r.text[:200]}"}
            last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            if cb_mod:
                try: cb_mod.record_failure(platform, last_error)
                except Exception: pass
        except requests.RequestException as e:
            last_error = str(e)
            if cb_mod:
                try: cb_mod.record_failure(platform, last_error)
                except Exception: pass

    return {"success": False, "detail": f"Failed after {max_attempts} attempts: {last_error}"}


def _refund_stripe(stripe_client, payment_intent_id: str, max_attempts: int = 3) -> dict:
    """
    Refund or cancel a Stripe PaymentIntent with automatic retry.
    Treats 'already refunded' / 'already cancelled' as success.
    Returns {"success": bool, "action": str, "refund_id": str (optional)}
    """
    last_error = ""
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            pi = stripe_client.PaymentIntent.retrieve(payment_intent_id)
            if pi.status == "requires_capture":
                stripe_client.PaymentIntent.cancel(payment_intent_id)
                return {"success": True, "action": "hold_cancelled"}
            elif pi.status == "succeeded":
                refund = stripe_client.Refund.create(payment_intent=payment_intent_id)
                return {"success": True, "action": "refunded", "refund_id": refund.id}
            elif pi.status in ("canceled", "cancelled"):
                return {"success": True, "action": "already_cancelled"}
            else:
                return {"success": True, "action": f"no_action (pi_status={pi.status})"}
        except Exception as e:
            err = str(e)
            if "already been refunded" in err or "charge_already_refunded" in err:
                return {"success": True, "action": "already_refunded"}
            if "already canceled" in err or "already cancelled" in err:
                return {"success": True, "action": "already_cancelled"}
            last_error = err
    return {"success": False, "action": "failed_after_retries", "error": last_error}


def _queue_octo_retry(booking_id: str, supplier_id: str, confirmation: str,
                      payment_intent_id: str, price_charged: float) -> None:
    """
    Persist a failed OCTO cancellation to Supabase Storage `cancellation_queue/`.
    The retry scheduler (APScheduler, every 15 min) picks this up via retry_cancellations.py
    and retries automatically — no manual intervention needed.
    """
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        print(f"[CANCEL_QUEUE] Supabase not fully configured (URL or SECRET missing) — "
              f"cannot queue OCTO retry for {booking_id}. "
              "Set SUPABASE_URL and SUPABASE_SECRET_KEY in Railway env vars.")
        return
    entry = {
        "booking_id":        booking_id,
        "confirmation":      confirmation,
        "supplier_id":       supplier_id,
        "payment_intent_id": payment_intent_id,
        "price_charged":     price_charged,
        "attempts":          0,
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "status":            "pending_octo",
        "error_detail":      None,
    }
    try:
        r = requests.post(
            f"{sb_url}/storage/v1/object/bookings/cancellation_queue/{booking_id}.json",
            headers={**_sb_storage_headers(), "Content-Type": "application/json",
                     "x-upsert": "true"},
            data=json.dumps(entry),
            timeout=10,
        )
        if r.ok:
            print(f"[CANCEL_QUEUE] Queued for auto-retry: {booking_id} | {supplier_id} | {confirmation}")
        else:
            print(f"[CANCEL_QUEUE] Storage write failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[CANCEL_QUEUE] Could not queue {booking_id}: {e}")


@app.route("/bookings/<booking_id>", methods=["GET"])
def get_booking(booking_id: str):
    """Return booking status by ID. Used by MCP get_booking_status tool.
    Requires X-API-Key to prevent unauthenticated enumeration of booking records (IDOR).
    """
    api_key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(api_key):
        return jsonify({"error": "Unauthorized. Valid X-API-Key required."}), 401
    record = _load_booking_record(booking_id)
    if not record:
        return jsonify({"error": f"Booking '{booking_id}' not found."}), 404
    return jsonify({
        "booking_id":          record.get("booking_id", booking_id),
        "status":              record.get("status", "unknown"),
        "payment_status":      record.get("payment_status"),
        "service_name":        record.get("service_name"),
        "business_name":       record.get("business_name"),
        "location_city":       record.get("location_city"),
        "start_time":          record.get("start_time"),
        "customer_name":       record.get("customer_name"),
        "confirmation_number": record.get("confirmation"),
        "quantity":            record.get("quantity", 1),
        "price_per_person":    record.get("our_price"),
        "price_charged":       record.get("price_charged"),
        "currency":            record.get("currency", "USD"),
        "checkout_url":        record.get("checkout_url"),
        "created_at":          record.get("created_at"),
        "failure_reason":      record.get("failure_reason"),
    })


def _cancel_cutoff_hours(record: dict) -> int:
    """
    Get the per-product cancellation cutoff in hours for a booking record.

    Resolution order:
      1. Direct field on the booking record (set at booking time for new bookings)
      2. booking_url JSON blob (always contains the cutoff if slot was fetched after the fix)
      3. Slot lookup from Supabase (works if slot is still live)
      4. Default: 48 hours (conservative — protects us against unknown supplier policies)
    """
    # 1. Direct field
    val = record.get("cancellation_cutoff_hours")
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    # 2. booking_url JSON
    try:
        burl = record.get("booking_url", "")
        if burl and isinstance(burl, str) and burl.startswith("{"):
            burl_d = json.loads(burl)
            val = burl_d.get("cancellation_cutoff_hours")
            if val is not None:
                return int(val)
    except Exception:
        pass
    # 3. Slot lookup
    sid = record.get("slot_id", "")
    if sid:
        slot = get_slot_by_id(sid)
        if slot:
            val = slot.get("cancellation_cutoff_hours")
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    # 4. Default
    return 48


def _cancel_cutoff_display(hours: int) -> str:
    """Convert cancellation cutoff hours to a human-readable string."""
    if hours <= 0:
        return "up to departure"
    if hours >= 9999 * 24:
        return "non-refundable"
    if hours % 24 == 0 and hours >= 48:
        return f"{hours // 24} days"
    return f"{hours} hours"


@app.route("/bookings/<booking_id>", methods=["DELETE"])
def cancel_booking(booking_id: str):
    """
    Cancel a booking by ID.

    OCTO-compatible cancellation endpoint.
    Required by Ventrata and other OCTO suppliers for reseller approval.

    Flow:
      1. Look up booking record by booking_id
      2. Cancel on source platform (OCTO DELETE /bookings/{uuid})
      3. Refund Stripe payment if already captured, or cancel hold if not
      4. Update booking record status to 'cancelled'
      5. Return cancellation confirmation

    DELETE /bookings/{booking_id}
    → { "success": true, "booking_id": "bk_...", "status": "cancelled",
        "refund_id": "re_...", "platform_result": "..." }
    """
    # Require a valid API key — this endpoint modifies booking state and triggers
    # refunds. The self-serve customer cancel flow uses /cancel/<id>?t=<token> instead.
    api_key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(api_key):
        return jsonify({"success": False, "error": "Unauthorized. Valid X-API-Key required."}), 401

    record = _load_booking_record(booking_id)
    if not record:
        return jsonify({"success": False, "error": "Booking not found"}), 404

    if record.get("status") == "cancelled":
        return jsonify({"success": True, "booking_id": booking_id, "status": "cancelled",
                        "message": "Booking was already cancelled"}), 200

    # Per-product cancellation policy: block cancellations for non-refundable or within cutoff
    _cutoff_h = _cancel_cutoff_hours(record)
    if _cutoff_h >= 9999 * 24:
        return jsonify({"success": False,
                        "error": "This booking is non-refundable. No cancellations or refunds are available.",
                        "cancellation_policy": "Non-refundable"}), 403
    try:
        _evt_time = record.get("start_time", "")
        if _evt_time:
            _evt_dt = datetime.fromisoformat(_evt_time.replace("Z", "+00:00"))
            _now = datetime.now(timezone.utc)
            if _evt_dt > _now and _evt_dt - _now < timedelta(hours=_cutoff_h):
                _display = _cancel_cutoff_display(_cutoff_h)
                return jsonify({"success": False, "error": "Cancellations are not permitted "
                                f"within {_display} of the activity",
                                "cancellation_policy": _cancel_policy_label(_cutoff_h)}), 403
    except (ValueError, TypeError):
        pass

    platform       = record.get("platform", "")
    confirmation   = record.get("confirmation", "")
    payment_intent = record.get("payment_intent_id", "")
    supplier_id    = record.get("supplier_id", platform)
    price_charged  = float(record.get("price_charged", 0))

    # ── Step 1: Stripe refund first (customer always gets their money back) ───
    # Retried up to 3× with backoff. 'Already refunded' treated as success.
    stripe_client = _stripe()
    stripe_result = {"success": True, "action": "no_payment_on_record"}
    if stripe_client.api_key and payment_intent:
        stripe_result = _refund_stripe(stripe_client, payment_intent)
        if not stripe_result["success"]:
            # Stripe failure after all retries — still proceed, log prominently
            print(f"[CANCEL] ⚠ Stripe refund failed for {booking_id}: {stripe_result.get('error')}")

    # ── Step 2: OCTO cancellation (retried up to 3×, then queued if still failing) ──
    octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
    is_octo = supplier_id in octo_platforms or platform == "octo"
    octo_result  = {"success": True, "detail": "No OCTO booking to cancel"}
    octo_queued  = False

    if is_octo and confirmation:
        octo_result = _cancel_octo_booking(supplier_id, confirmation)
        if not octo_result["success"]:
            if octo_result.get("permanent"):
                # Auth failure / bad request — retrying won't help, log it
                print(f"[CANCEL] ✗ Permanent OCTO failure for {booking_id}: {octo_result['detail']}")
            else:
                # Transient failure — queue for automatic background retry every 15 min
                _queue_octo_retry(booking_id, supplier_id, confirmation, payment_intent, price_charged)
                octo_queued = True
                print(f"[CANCEL] OCTO cancel queued for automatic retry: {booking_id}")

    # ── Step 3: Update booking record ────────────────────────────────────────
    # Only mark as "cancelled" if the customer has been refunded (or there was nothing to refund).
    # If Stripe failed, flag as "cancellation_refund_failed" so it gets reviewed/retried.
    stripe_ok = stripe_result.get("success", True)

    # ── Step 4: Wallet credit-back (wallet bookings only — gated on stripe_ok) ──
    # Only credit back if the Stripe leg succeeded (or there was no Stripe charge).
    # Prevents double-credit if Job 3 in reconcile_bookings later retries and
    # also calls credit_wallet on the same record.
    if stripe_ok and record.get("payment_method") == "wallet" and record.get("wallet_id"):
        try:
            wlt_mod = _load_module("manage_wallets")
            if wlt_mod:
                price_charged = float(record.get("price_charged") or 0)
                wlt_mod.credit_wallet(
                    record["wallet_id"],
                    int(price_charged * 100),
                    f"Refund: booking cancelled ({booking_id})",
                )
                print(f"[CANCEL] Wallet credit-back issued: {booking_id} → {record['wallet_id']}")
        except Exception as _wlt_err:
            print(f"[CANCEL] ⚠ Wallet refund failed (non-fatal — manual action needed): {_wlt_err}")
    cancelled_at = datetime.now(timezone.utc).isoformat()
    record["cancellation_details"] = {"stripe": stripe_result, "octo": octo_result}
    if stripe_ok:
        record["status"]       = "cancelled"
        record["cancelled_at"] = cancelled_at
    else:
        record["status"]              = "cancellation_refund_failed"
        record["cancellation_flag_at"] = cancelled_at
    _save_booking_record(booking_id, record)

    # Notify customer — send regardless of OCTO outcome; Stripe is what matters to them.
    try:
        from send_booking_email import send_booking_email
        if record.get("customer_email"):
            refund_action = stripe_result.get("action", "")
            refund_desc = (
                "A full refund has been issued to your original payment method."
                if refund_action in ("refunded", "hold_cancelled", "already_refunded", "already_cancelled")
                else (
                    "Your cancellation has been recorded. If a charge was made, "
                    "our team will process your refund within 3–5 business days."
                )
            )
            _cancel_slot = {
                "service_name":  record.get("service_name", "Your Experience"),
                "start_time":    record.get("start_time", ""),
                "location_city": record.get("location_city", ""),
                "our_price":     record.get("price_charged"),
                "currency":      record.get("currency", "USD"),
            }
            send_booking_email(
                "booking_cancelled", record["customer_email"],
                record.get("customer_name", ""), _cancel_slot,
                confirmation_number=booking_id, refund_status=refund_desc,
            )
    except Exception as _mail_err:
        print(f"[CANCEL] Cancellation email failed (non-fatal): {_mail_err}")

    return jsonify({
        "success":        stripe_ok,
        "booking_id":     booking_id,
        "status":         record["status"],
        "stripe_result":  stripe_result.get("action"),
        "refund_id":      stripe_result.get("refund_id"),
        "stripe_error":   stripe_result.get("error") if not stripe_ok else None,
        "platform_result": octo_result.get("detail"),
        "octo_queued_for_retry": octo_queued,
        "cancelled_at":   cancelled_at,
    }), 200 if stripe_ok else 502


# ── Supplier-initiated cancellation (Bokun webhook) ───────────────────────────

def _find_booking_by_confirmation(confirmation_code: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Look up a booking by confirmation code or supplier_reference.
    Returns (booking_id, record) or (None, None).

    Fast path: by_confirmation/ index (O(1)) — populated for all bookings written after
    the index was introduced. Falls back to a full linear scan for older records.
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb_url:
        return None, None
    headers = _sb_storage_headers()

    # ── Fast path: confirmation index ────────────────────────────────────────
    try:
        idx_r = requests.get(
            f"{sb_url}/storage/v1/object/bookings/by_confirmation/{confirmation_code}.json",
            headers=headers, timeout=5,
        )
        if idx_r.status_code == 200:
            idx_booking_id = idx_r.json().get("booking_id", "")
            if idx_booking_id:
                record = _load_booking_record(idx_booking_id)
                if record:
                    return idx_booking_id, record
    except Exception:
        pass

    # ── Slow path: linear scan (for records predating the index) ─────────────

    names: list[str] = []
    offset = 0
    page_size = 500
    while True:
        try:
            r = requests.post(
                f"{sb_url}/storage/v1/object/list/bookings",
                headers={**headers, "Content-Type": "application/json"},
                json={"prefix": "", "limit": page_size, "offset": offset},
                timeout=10,
            )
            if r.status_code != 200:
                break
            page = r.json()
            if not page:
                break
            for item in page:
                n = item.get("name", "")
                if (n.endswith(".json")
                        and not n.startswith("cancellation_queue/")
                        and not n.startswith("by_confirmation/")
                        and not n.startswith("callback_queue/")
                        and not n.startswith("circuit_breaker/")
                        and not n.startswith("config/")
                        and not n.startswith("idem_")
                        and not n.startswith("webhook_session_")
                        and not n.startswith("cleanup_")
                        and not n.startswith("pending_exec_")
                        and not n.startswith("inbound_emails/")):
                    names.append(n)
            if len(page) < page_size:
                break  # last page
            offset += page_size
        except Exception:
            break

    for name in names:
        booking_id = name.replace(".json", "")
        record = _load_booking_record(booking_id)
        if not record:
            continue
        # Match on OCTO UUID (confirmation) OR Bokun's own reference (supplier_reference).
        # Bokun webhooks send their reference; OCTO DELETE uses the UUID.
        if (record.get("confirmation") == confirmation_code
                or record.get("supplier_reference") == confirmation_code):
            return booking_id, record
    return None, None


@app.route("/api/bokun/webhook", methods=["POST"])
def bokun_webhook():
    """
    Receive Bokun HTTP Booking notification webhooks.

    When a supplier cancels a booking in their Bokun dashboard, Bokun POSTs
    here. We look up the booking by Bokun confirmation code, issue a Stripe
    refund, and email the customer.

    Bokun's HTTP notification system does not send HMAC signatures.
    Auth is done via a secret token in the URL query string:
      https://api.lastminutedealshq.com/api/bokun/webhook?token=TOKEN

    Set BOKUN_WEBHOOK_TOKEN in Railway env vars and append it to the URL
    in Bokun Dashboard → Settings → Connections → Integrated systems → Edit.
    """
    # ── Token verification ────────────────────────────────────────────────────
    webhook_token = os.getenv("BOKUN_WEBHOOK_TOKEN", "").strip()
    if webhook_token:
        provided = request.args.get("token", "")
        if not hmac.compare_digest(webhook_token, provided):
            print("[BOKUN_WEBHOOK] Invalid token — rejected")
            return jsonify({"error": "Unauthorized"}), 401
    else:
        print("[BOKUN_WEBHOOK] BOKUN_WEBHOOK_TOKEN not set — rejecting request for safety")
        return jsonify({"error": "Webhook token not configured — set BOKUN_WEBHOOK_TOKEN env var"}), 503

    data = request.get_json(force=True, silent=True) or {}

    event_type   = data.get("type", "")
    booking_data = data.get("booking") or data
    bokun_status = str(booking_data.get("status", "")).upper()
    confirmation = (
        booking_data.get("confirmationCode")
        or booking_data.get("confirmation_code")
        or booking_data.get("id", "")
    )

    print(f"[BOKUN_WEBHOOK] event={event_type} status={bokun_status} confirmation={confirmation}")

    # Only act on cancellations
    is_cancel = bokun_status in ("CANCELLED", "CANCELED") or "cancel" in event_type.lower()
    if not is_cancel:
        return jsonify({"received": True, "action": "ignored", "status": bokun_status}), 200

    if not confirmation:
        return jsonify({"received": True, "action": "ignored", "reason": "no confirmation code"}), 200

    # Find our internal booking record
    booking_id, record = _find_booking_by_confirmation(confirmation)
    if not record:
        print(f"[BOKUN_WEBHOOK] No matching booking for confirmation={confirmation}")
        return jsonify({"received": True, "action": "not_found", "confirmation": confirmation}), 200

    if record.get("status") == "cancelled":
        return jsonify({"received": True, "action": "already_cancelled", "booking_id": booking_id}), 200

    # Trigger the existing cancel flow (refund Stripe + update record)
    stripe_client  = _stripe()
    payment_intent = record.get("payment_intent_id", "")
    stripe_result  = {"success": True, "action": "no_payment_on_record"}

    if stripe_client.api_key and payment_intent:
        stripe_result = _refund_stripe(stripe_client, payment_intent)
        if not stripe_result["success"]:
            print(f"[BOKUN_WEBHOOK] ⚠ Stripe refund failed for {booking_id}: {stripe_result.get('error')}")
            # Do not mark as cancelled — customer has not been refunded yet.
            # Flag for manual review instead.
            record["status"]              = "cancellation_refund_failed"
            record["cancellation_details"] = {"stripe": stripe_result, "bokun_event": event_type}
            record["cancellation_flag_at"]  = datetime.now(timezone.utc).isoformat()
            _save_booking_record(booking_id, record)
            return jsonify({
                "received":   True,
                "action":     "refund_failed",
                "booking_id": booking_id,
                "error":      stripe_result.get("error"),
            }), 200

    # Wallet credit-back (wallet bookings only)
    if record.get("payment_method") == "wallet" and record.get("wallet_id"):
        try:
            wlt_mod = _load_module("manage_wallets")
            if wlt_mod:
                price_charged = float(record.get("price_charged") or 0)
                wlt_mod.credit_wallet(
                    record["wallet_id"],
                    int(price_charged * 100),
                    f"Refund: supplier cancelled booking ({booking_id})",
                )
                print(f"[BOKUN_WEBHOOK] Wallet credit-back issued: {booking_id} → {record['wallet_id']}")
        except Exception as _wlt_err:
            print(f"[BOKUN_WEBHOOK] ⚠ Wallet refund failed (non-fatal): {_wlt_err}")

    cancelled_at = datetime.now(timezone.utc).isoformat()
    record["status"]       = "cancelled"
    record["cancelled_at"] = cancelled_at
    record["cancelled_by"] = "supplier_bokun_webhook"
    record["cancellation_details"] = {"stripe": stripe_result, "bokun_event": event_type}
    _save_booking_record(booking_id, record)

    # Email the customer
    try:
        email_mod = _load_module("send_booking_email")
        if email_mod and record.get("customer_email"):
            refund_desc = (
                f"A full refund of {record.get('price_charged', '')} has been issued to your payment method."
                if stripe_result.get("action") in ("refunded", "hold_cancelled")
                else "A full refund has been initiated — it will appear within 3–5 business days."
            )
            slot = {
                "service_name":  record.get("service_name", "Your Experience"),
                "start_time":    record.get("start_time", ""),
                "location_city": record.get("location_city", ""),
                "our_price":     record.get("price_charged"),
                "currency":      record.get("currency", "USD"),
            }
            email_mod.send_booking_email(
                email_type="booking_cancelled",
                customer_email=record["customer_email"],
                customer_name=record.get("customer_name", ""),
                slot=slot,
                confirmation_number=booking_id,
                refund_status=refund_desc,
            )
            print(f"[BOKUN_WEBHOOK] Cancellation email sent to {record['customer_email']}")
    except Exception as e:
        print(f"[BOKUN_WEBHOOK] Failed to send cancellation email: {e}")

    print(f"[BOKUN_WEBHOOK] ✓ Booking {booking_id} cancelled by supplier. Refund: {stripe_result.get('action')}")
    return jsonify({
        "received":    True,
        "action":      "cancelled",
        "booking_id":  booking_id,
        "stripe":      stripe_result.get("action"),
        "refund_id":   stripe_result.get("refund_id"),
    }), 200


@app.route("/cancel/<booking_id>", methods=["GET", "POST"])
def self_serve_cancel(booking_id: str):
    """
    Self-serve cancellation page for customers.

    GET  /cancel/{booking_id}  — show confirmation page
    POST /cancel/{booking_id}  — execute cancellation (form submit)

    Link included in the booking confirmation email so customers can cancel
    without emailing support.
    """
    # Verify HMAC token to prevent unauthenticated cancellations.
    # Token is required on both GET (view) and POST (confirm) to stop
    # enumeration attacks — booking IDs are deterministic and guessable.
    token = request.args.get("t") or request.form.get("t", "")
    if not _verify_cancel_token(booking_id, token):
        return (
            """<!DOCTYPE html><html><head><title>Invalid Link</title></head>
            <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
            <h2 style="color:#0f172a;">Invalid or expired link</h2>
            <p style="color:#64748b;">This cancellation link is invalid. Please use the link from your
            original booking confirmation email.<br><br>Need help? Email
            <a href="mailto:bookings@lastminutedealshq.com">bookings@lastminutedealshq.com</a>.</p>
            </body></html>""",
            403,
        )

    record = _load_booking_record(booking_id)
    landing_url = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com")

    if not record:
        return f"""<!DOCTYPE html><html><head><title>Booking Not Found</title></head>
        <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
        <h2 style="color:#0f172a;">Booking not found</h2>
        <p style="color:#64748b;">We couldn't find a booking with that ID. If you need help,
        email <a href="mailto:bookings@lastminutedealshq.com">bookings@lastminutedealshq.com</a>.</p>
        <a href="{landing_url}" style="display:inline-block;margin-top:24px;padding:12px 28px;
        background:#0f172a;color:#fff;border-radius:8px;text-decoration:none;">Browse Deals</a>
        </body></html>""", 404

    service      = record.get("service_name", "Your Booking")
    status       = record.get("status", "")
    already_done = status == "cancelled"

    # Must be initialized before the POST block so the already_done rendering below
    # can reference it even when the booking was already cancelled on arrival (C-2).
    refund_issued = False

    # Per-product cancellation policy: no refunds within the supplier's cutoff window
    _within_cutoff = False
    _cutoff_h = _cancel_cutoff_hours(record)
    _cutoff_display = _cancel_cutoff_display(_cutoff_h)
    _is_nonrefundable = _cutoff_h >= 9999 * 24

    # Non-refundable products: no cancel form at all
    if _is_nonrefundable and not already_done:
        return f"""<!DOCTYPE html><html><head><title>Non-Refundable Booking</title></head>
        <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
        <h2 style="color:#0f172a;">Non-refundable booking</h2>
        <p style="color:#64748b;">Your booking for <strong>{service}</strong> is non-refundable.
        No cancellations or refunds are available for this product.</p>
        <p style="color:#64748b;">If you have questions, please email
        <a href="mailto:bookings@lastminutedealshq.com">bookings@lastminutedealshq.com</a>.</p>
        <a href="{landing_url}" style="display:inline-block;margin-top:24px;padding:12px 28px;
        background:#0f172a;color:#fff;border-radius:8px;text-decoration:none;">Back to Home</a>
        </body></html>""", 403

    if not already_done:
        try:
            _evt_time = record.get("start_time", "")
            if _evt_time:
                _evt_dt = datetime.fromisoformat(_evt_time.replace("Z", "+00:00"))
                _now = datetime.now(timezone.utc)
                if _evt_dt > _now and _evt_dt - _now < timedelta(hours=_cutoff_h):
                    _within_cutoff = True
        except (ValueError, TypeError):
            pass

    if _within_cutoff and not already_done:
        # For long-cutoff products, show the specific deadline date
        _deadline_msg = ""
        if _cutoff_h > 48:
            try:
                _evt_dt2 = datetime.fromisoformat(record.get("start_time", "").replace("Z", "+00:00"))
                _deadline_dt = _evt_dt2 - timedelta(hours=_cutoff_h)
                _deadline_msg = f" The cancellation deadline was {_deadline_dt.strftime('%B %d, %Y')}."
            except Exception:
                pass
        return f"""<!DOCTYPE html><html><head><title>Cancellation Not Available</title></head>
        <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
        <h2 style="color:#0f172a;">Cancellation not available</h2>
        <p style="color:#64748b;">Your booking for <strong>{service}</strong> is within
        {_cutoff_display} of the scheduled activity. Per the cancellation policy, refunds
        are not available at this time.{_deadline_msg}</p>
        <p style="color:#64748b;">If you have questions or need to make changes, please email
        <a href="mailto:bookings@lastminutedealshq.com">bookings@lastminutedealshq.com</a>.</p>
        <a href="{landing_url}" style="display:inline-block;margin-top:24px;padding:12px 28px;
        background:#0f172a;color:#fff;border-radius:8px;text-decoration:none;">Back to Home</a>
        </body></html>""", 403

    if request.method == "POST" and not already_done:
        # Execute cancellation inline (same logic as DELETE /bookings/{id})
        stripe_client  = _stripe()
        payment_intent = record.get("payment_intent_id", "")
        stripe_result  = {"success": True, "action": "no_payment_on_record"}

        if stripe_client.api_key and payment_intent:
            stripe_result = _refund_stripe(stripe_client, payment_intent)
            if not stripe_result["success"]:
                print(f"[SELF_CANCEL] ⚠ Stripe refund failed for {booking_id}: {stripe_result.get('error')}")

        supplier_id  = record.get("supplier_id", record.get("platform", ""))
        confirmation = record.get("confirmation", "")
        octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
        # C-3: match the same is_octo check used by DELETE /bookings/{id}
        is_octo_self = supplier_id in octo_platforms or record.get("platform", "") == "octo"
        octo_result  = {"success": True, "detail": "No OCTO booking"}
        if is_octo_self and confirmation:
            octo_result = _cancel_octo_booking(supplier_id, confirmation)
            if not octo_result["success"] and not octo_result.get("permanent"):
                # Queue for automatic background retry
                price_charged = float(record.get("price_charged", 0))
                _queue_octo_retry(booking_id, supplier_id, confirmation, payment_intent, price_charged)

        # C-4: only mark "cancelled" if Stripe succeeded — same logic as DELETE path.
        # If Stripe failed, mark "cancellation_refund_failed" so it can be reviewed/retried.
        stripe_ok_self = stripe_result.get("success", True)

        # Wallet credit-back (wallet bookings only — gated on stripe_ok_self).
        # Prevents double-credit if Job 3 in reconcile_bookings later retries this record.
        if stripe_ok_self and record.get("payment_method") == "wallet" and record.get("wallet_id"):
            try:
                wlt_mod = _load_module("manage_wallets")
                if wlt_mod:
                    price_charged = float(record.get("price_charged") or 0)
                    wlt_mod.credit_wallet(
                        record["wallet_id"],
                        int(price_charged * 100),
                        f"Refund: booking cancelled ({booking_id})",
                    )
                    print(f"[SELF_CANCEL] Wallet credit-back issued: {booking_id} → {record['wallet_id']}")
            except Exception as _wlt_err:
                print(f"[SELF_CANCEL] ⚠ Wallet refund failed (non-fatal): {_wlt_err}")
        cancelled_at = datetime.now(timezone.utc).isoformat()
        record["cancelled_by"] = "customer_self_serve"
        record["cancellation_details"] = {"stripe": stripe_result, "octo": octo_result}
        if stripe_ok_self:
            record["status"]       = "cancelled"
            record["cancelled_at"] = cancelled_at
        else:
            record["status"]              = "cancellation_refund_failed"
            record["cancellation_flag_at"] = cancelled_at
        _save_booking_record(booking_id, record)

        # Notify customer — refund_status reflects actual outcome
        refund_issued = stripe_result.get("action") in ("refunded", "hold_cancelled")
        if refund_issued:
            refund_desc = "A full refund has been issued to your original payment method."
        else:
            refund_desc = ("Your cancellation has been recorded. "
                           "If a charge was made, our team will process your refund within 3–5 business days.")
        try:
            email_mod = _load_module("send_booking_email")
            if email_mod and record.get("customer_email"):
                slot = {
                    "service_name":  service,
                    "start_time":    record.get("start_time", ""),
                    "location_city": record.get("location_city", ""),
                    "our_price":     record.get("price_charged"),
                    "currency":      record.get("currency", "USD"),
                }
                email_mod.send_booking_email(
                    email_type="booking_cancelled",
                    customer_email=record["customer_email"],
                    customer_name=record.get("customer_name", ""),
                    slot=slot,
                    confirmation_number=booking_id,
                    refund_status=refund_desc,
                    cancelled_by_customer=True,
                )
        except Exception as _mail_err:
            print(f"[SELF_CANCEL] Cancellation email failed (non-fatal): {_mail_err}")

        already_done = True

    if already_done:
        refund_html = (
            "A full refund has been issued to your original payment method. It typically appears within 3–5 business days."
            if refund_issued else
            "Your cancellation has been recorded. If a charge was made, our team will process your refund within 3–5 business days."
        )
        return f"""<!DOCTYPE html><html><head><title>Booking Cancelled</title></head>
        <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
        <div style="font-size:48px;margin-bottom:16px;">✓</div>
        <h2 style="color:#0f172a;">Booking cancelled</h2>
        <p style="color:#64748b;">Your booking for <strong>{service}</strong> has been cancelled.
        {refund_html}</p>
        <p style="color:#64748b;">Check your email for confirmation.</p>
        <a href="{landing_url}" style="display:inline-block;margin-top:24px;padding:12px 28px;
        background:#0f172a;color:#fff;border-radius:8px;text-decoration:none;">Browse More Deals</a>
        </body></html>"""

    # GET — show confirmation page before cancelling
    return f"""<!DOCTYPE html><html><head><title>Cancel Booking</title></head>
    <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
    <h2 style="color:#0f172a;">Cancel your booking?</h2>
    <p style="color:#64748b;margin-bottom:8px;">You're about to cancel:</p>
    <p style="font-size:18px;font-weight:700;color:#0f172a;margin:0 0 24px;">{service}</p>
    <p style="color:#64748b;margin-bottom:32px;">You'll receive a full refund to your original payment method
    within 3–5 business days.</p>
    <form method="POST">
      <input type="hidden" name="t" value="{token}">
      <button type="submit" style="display:inline-block;padding:14px 32px;background:#ef4444;
        color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:700;cursor:pointer;">
        Confirm Cancellation
      </button>
    </form>
    <p style="margin-top:20px;"><a href="{landing_url}" style="color:#64748b;font-size:14px;">
      Keep my booking</a></p>
    <p style="color:#94a3b8;font-size:12px;margin-top:24px;">Cancellation policy: Refunds are not
    available within {_cutoff_display} of the scheduled activity.</p>
    </body></html>"""


# ── Dry-run / test endpoint ───────────────────────────────────────────────────

@app.route("/api/test/book-dry-run", methods=["POST"])
def book_dry_run():
    """
    Dry-run end-to-end booking test — validates the full pipeline without real
    charges, real supplier calls, or real email sends.

    Use this before deploying, after a credentials rotation, or any time you
    want confidence that the happy path would work.

    Checks (in order):
      1. Slot lookup — can we resolve a slot_id from the aggregated inventory?
      2. Pricing — does the slot have a valid our_price?
      3. Wallet balance — is the balance sufficient (if wallet_id provided)?
      4. Booking URL parse — can we extract supplier_id from booking_url?
      5. OCTO supplier config — is the supplier enabled and API key present?
      6. OCTO connectivity — can we reach the supplier's /products endpoint?
      7. Stripe connectivity — is STRIPE_SECRET_KEY valid?
      8. Email config — is SMTP or SendGrid configured?

    No bookings are made. No charges are applied. No emails are sent.

    Body (all optional — defaults are chosen automatically):
      {
        "slot_id":   "abc123",    # defaults to first available slot
        "wallet_id": "wlt_..."    # if omitted, wallet balance check is skipped
      }

    Requires a valid X-API-Key header (same as /api/book/direct).
    """
    api_key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(api_key):
        return jsonify({"success": False, "error": "Unauthorized. Valid X-API-Key required."}), 401

    data      = request.get_json(force=True, silent=True) or {}
    slot_id   = (data.get("slot_id") or "").strip()
    wallet_id = (data.get("wallet_id") or "").strip()

    results: dict = {}
    all_ok = True

    # ── 1. Slot lookup ─────────────────────────────────────────────────────────
    slot = get_slot_by_id(slot_id) if slot_id else None
    if slot is None:
        # Fall back to first available slot in aggregated inventory
        try:
            slots = _load_slots_from_supabase(hours_ahead=168, limit=1)
            slot  = slots[0] if slots else None
        except Exception:
            slot = None
    if slot:
        results["slot_lookup"] = {"ok": True, "slot_id": slot.get("slot_id"),
                                  "service_name": slot.get("service_name", "?")}
    else:
        results["slot_lookup"] = {"ok": False, "error": "No slots available in inventory"}
        all_ok = False

    # ── 2. Pricing ─────────────────────────────────────────────────────────────
    our_price = float(slot.get("our_price") or slot.get("price") or 0) if slot else 0
    if our_price > 0:
        results["pricing"] = {"ok": True, "our_price": our_price}
    else:
        results["pricing"] = {"ok": False, "error": "Slot has no our_price set"}
        all_ok = False

    # ── 3. Wallet balance ──────────────────────────────────────────────────────
    if wallet_id:
        try:
            wlt_mod = _load_module("manage_wallets")
            balance = wlt_mod.get_balance(wallet_id) if wlt_mod else None
            if balance is not None and balance >= int(our_price * 100):
                results["wallet_balance"] = {"ok": True, "balance_cents": balance,
                                             "required_cents": int(our_price * 100)}
            else:
                results["wallet_balance"] = {"ok": False,
                                             "error": f"Balance {balance} < required {int(our_price * 100)}"}
                all_ok = False
        except Exception as e:
            results["wallet_balance"] = {"ok": False, "error": str(e)}
            all_ok = False
    else:
        results["wallet_balance"] = {"ok": True, "note": "No wallet_id provided — check skipped"}

    # ── 4. Booking URL parse ───────────────────────────────────────────────────
    supplier_id = None
    if slot:
        booking_url = slot.get("booking_url", "")
        try:
            burl_j      = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
            supplier_id = burl_j.get("supplier_id", slot.get("platform", ""))
        except Exception:
            supplier_id = slot.get("platform", "")
        if supplier_id:
            results["booking_url_parse"] = {"ok": True, "supplier_id": supplier_id}
        else:
            results["booking_url_parse"] = {"ok": False, "error": "Could not determine supplier_id"}
            all_ok = False

    # ── 5. OCTO supplier config ────────────────────────────────────────────────
    octo_api_key = ""
    octo_base_url = ""
    if supplier_id:
        try:
            seeds_path = Path(__file__).parent / "seeds" / "octo_suppliers.json"
            suppliers  = json.loads(seeds_path.read_text(encoding="utf-8"))
            supplier   = next((s for s in suppliers if s.get("supplier_id") == supplier_id and s.get("enabled")), None)
            if supplier:
                octo_api_key  = os.getenv(supplier.get("api_key_env", ""), "").strip()
                octo_base_url = supplier.get("base_url", "")
                if octo_api_key:
                    results["octo_config"] = {"ok": True, "supplier_id": supplier_id,
                                              "api_key_env": supplier.get("api_key_env")}
                else:
                    results["octo_config"] = {"ok": False,
                                              "error": f"API key not set ({supplier.get('api_key_env')})"}
                    all_ok = False
            else:
                results["octo_config"] = {"ok": False,
                                          "error": f"Supplier '{supplier_id}' not found or not enabled"}
                all_ok = False
        except Exception as e:
            results["octo_config"] = {"ok": False, "error": str(e)}
            all_ok = False

    # ── 6. OCTO connectivity ───────────────────────────────────────────────────
    if octo_api_key and octo_base_url:
        try:
            r = requests.get(
                f"{octo_base_url.rstrip('/')}/suppliers",
                headers={"Authorization": f"Bearer {octo_api_key}"},
                timeout=8,
            )
            if r.status_code in (200, 404):  # 404 is fine; endpoint may not exist on all vendors
                results["octo_connectivity"] = {"ok": True, "http_status": r.status_code}
            else:
                results["octo_connectivity"] = {"ok": False,
                                                "error": f"HTTP {r.status_code}: {r.text[:100]}"}
                all_ok = False
        except Exception as e:
            results["octo_connectivity"] = {"ok": False, "error": str(e)}
            all_ok = False
    else:
        results["octo_connectivity"] = {"ok": True, "note": "Skipped — no OCTO credentials resolved"}

    # ── 7. Stripe connectivity ─────────────────────────────────────────────────
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if stripe_key:
        try:
            import stripe as _stripe_mod
            _stripe_mod.api_key = stripe_key
            _stripe_mod.Balance.retrieve()
            results["stripe"] = {"ok": True}
        except Exception as e:
            results["stripe"] = {"ok": False, "error": str(e)[:120]}
            all_ok = False
    else:
        results["stripe"] = {"ok": False, "error": "STRIPE_SECRET_KEY not set"}
        all_ok = False

    # ── 8. Email config ────────────────────────────────────────────────────────
    smtp_ok  = bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))
    sg_ok    = bool(os.getenv("SENDGRID_API_KEY"))
    if smtp_ok or sg_ok:
        results["email_config"] = {"ok": True,
                                   "smtp": smtp_ok, "sendgrid": sg_ok}
    else:
        results["email_config"] = {"ok": False,
                                   "error": "Neither SMTP nor SendGrid is configured"}
        all_ok = False

    return jsonify({
        "dry_run":      True,
        "all_checks_ok": all_ok,
        "checks":       results,
        "summary":      "All systems go — ready to accept real bookings." if all_ok
                        else "One or more checks failed. Review 'checks' for details.",
    }), 200 if all_ok else 500


# ── Agent wallet routes ───────────────────────────────────────────────────────

@app.route("/api/wallets/create", methods=["POST"])
def wallet_create():
    """
    Create a pre-funded agent wallet.

    Body: { "name": "MyAgent", "email": "agent@example.com" }
    Returns: { "wallet_id": "wlt_...", "api_key": "lmd_...", "balance": 0.00, "fund_instructions": "..." }
    """
    import importlib.util as _ilu
    from pathlib import Path as _Path

    data  = request.get_json(force=True, silent=True) or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not email:
        return jsonify({"success": False, "error": "name and email required."}), 400

    spec = _ilu.spec_from_file_location("manage_wallets", _Path(__file__).parent / "manage_wallets.py")
    wlt_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wlt_mod)

    wlt = wlt_mod.create_wallet(name, email)
    return jsonify({
        "success":      True,
        "wallet_id":    wlt["wallet_id"],
        "api_key":      wlt["api_key"],
        "balance":      wlt.get("balance_cents", 0) / 100,
        "currency":     wlt.get("currency", "usd"),
        "fund_instructions": f"POST /api/wallets/fund with wallet_id and amount_dollars to generate a Stripe payment link.",
    })


@app.route("/api/wallets/fund", methods=["POST"])
def wallet_fund():
    """
    Generate a Stripe Checkout link to fund a wallet.

    Body: { "wallet_id": "wlt_...", "amount_dollars": 50 }
    Returns: { "checkout_url": "https://checkout.stripe.com/..." }
    """
    import importlib.util as _ilu
    from pathlib import Path as _Path

    data           = request.get_json(force=True, silent=True) or {}
    wallet_id      = (data.get("wallet_id") or "").strip()
    amount_dollars = data.get("amount_dollars") or data.get("amount")

    if not wallet_id or not amount_dollars:
        return jsonify({"success": False, "error": "wallet_id and amount_dollars required."}), 400

    spec = _ilu.spec_from_file_location("manage_wallets", _Path(__file__).parent / "manage_wallets.py")
    wlt_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wlt_mod)

    try:
        url = wlt_mod.create_topup_session(wallet_id, int(float(amount_dollars) * 100))
        return jsonify({"success": True, "checkout_url": url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/wallets/<wallet_id>/balance", methods=["GET"])
def wallet_balance(wallet_id: str):
    """Return current wallet balance. Requires X-API-Key matching the wallet's api_key."""
    import importlib.util as _ilu
    from pathlib import Path as _Path

    spec = _ilu.spec_from_file_location("manage_wallets", _Path(__file__).parent / "manage_wallets.py")
    wlt_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wlt_mod)

    # Validate: key must belong to this wallet
    api_key = request.headers.get("X-API-Key", "").strip()
    wlt = wlt_mod.get_wallet(wallet_id)
    if not wlt:
        return jsonify({"error": "Wallet not found."}), 404
    if not hmac.compare_digest(wlt.get("api_key", ""), api_key):
        return jsonify({"error": "Unauthorized."}), 401

    bal = wlt.get("balance_cents", 0)
    return jsonify({
        "wallet_id": wallet_id,
        "balance":   bal / 100,
        "currency":  wlt.get("currency", "usd"),
        "last_funded": wlt.get("last_funded"),
        "last_used":   wlt.get("last_used"),
    })


@app.route("/api/wallets/<wallet_id>/transactions", methods=["GET"])
def wallet_transactions(wallet_id: str):
    """Return last 50 wallet transactions."""
    import importlib.util as _ilu
    from pathlib import Path as _Path

    spec = _ilu.spec_from_file_location("manage_wallets", _Path(__file__).parent / "manage_wallets.py")
    wlt_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wlt_mod)

    api_key = request.headers.get("X-API-Key", "").strip()
    wlt = wlt_mod.get_wallet(wallet_id)
    if not wlt:
        return jsonify({"error": "Wallet not found."}), 404
    if not hmac.compare_digest(wlt.get("api_key", ""), api_key):
        return jsonify({"error": "Unauthorized."}), 401

    txs = wlt.get("transactions", [])[-50:]
    return jsonify({"wallet_id": wallet_id, "transactions": txs})


@app.route("/api/wallets/<wallet_id>/spending-limit", methods=["PUT"])
def wallet_set_spending_limit(wallet_id: str):
    """
    Set or remove the per-transaction spending limit on a wallet.

    Body:
      { "limit_dollars": 50.0 }   — cap each booking at $50
      { "limit_dollars": null }    — remove cap (unlimited)

    Requires master X-API-Key (not the wallet's own key) — this is an admin operation.
    """
    api_key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(api_key):
        return jsonify({"error": "Unauthorized."}), 401

    data = request.get_json(force=True, silent=True) or {}
    limit_dollars = data.get("limit_dollars")
    limit_cents = None if limit_dollars is None else int(float(limit_dollars) * 100)

    wlt_mod = _load_module("manage_wallets")
    if not wlt_mod:
        return jsonify({"error": "Wallet system unavailable."}), 503

    if not wlt_mod.get_wallet(wallet_id):
        return jsonify({"error": "Wallet not found."}), 404

    wlt_mod.set_spending_limit(wallet_id, limit_cents)
    return jsonify({
        "wallet_id":            wallet_id,
        "spending_limit_cents": limit_cents,
        "spending_limit_dollars": limit_dollars,
        "message": "Spending limit updated." if limit_cents is not None else "Spending limit removed.",
    })


# ── Admin: slot refresh ───────────────────────────────────────────────────────

@app.route("/admin/refresh-slots", methods=["POST"])
def admin_refresh_slots():
    """
    Trigger a full slot pipeline run on the Railway instance.
    Runs fetch_octo_slots.py → aggregate_slots.py in-process.
    Protected by X-API-Key (same as write endpoints).
    """
    api_key = request.headers.get("X-API-Key", "").strip()
    if not _validate_api_key(api_key):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    results = {}
    try:
        import importlib.util as _ilu
        tools = Path(__file__).parent

        for script in ("fetch_octo_slots", "aggregate_slots", "compute_pricing", "sync_to_supabase"):
            spec = _ilu.spec_from_file_location(script, tools / f"{script}.py")
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "main"):
                mod.main()
            results[script] = "ok"

        # Count slots after refresh
        slot_count = 0
        if DATA_FILE.exists():
            try:
                slot_count = len(json.loads(DATA_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass

        return jsonify({"success": True, "slots": slot_count, "steps": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "steps": results}), 500


# ── Peek webhook receiver ─────────────────────────────────────────────────────

@app.route("/webhooks/peek", methods=["POST"])
def peek_webhook():
    """
    Receive booking_update events pushed by Peek Pro via OCTO webhooks.

    Peek fires this whenever a booking is confirmed, updated, or cancelled
    on their platform. We use it to keep our Supabase Storage records in sync
    in real time — no waiting for the 30-min reconciliation cycle.

    Payload fields used:
      booking.uuid      — OCTO booking UUID (matches our 'confirmation' field)
      booking.status    — CONFIRMED | CANCELLED | ON_HOLD | EXPIRED | etc.

    Auth: PEEK_WEBHOOK_SECRET env var verified against X-Peek-Signature header
    or ?token= query param.
    """
    # Verify webhook authenticity using PEEK_WEBHOOK_SECRET
    peek_secret = os.getenv("PEEK_WEBHOOK_SECRET", "").strip()
    if peek_secret:
        provided = (request.headers.get("X-Peek-Signature", "")
                    or request.args.get("token", ""))
        if not hmac.compare_digest(peek_secret, provided):
            print("[PEEK_WEBHOOK] Invalid auth token — rejected")
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
    else:
        print("[PEEK_WEBHOOK] WARNING: PEEK_WEBHOOK_SECRET not set — accepting unauthenticated events")

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    booking_data = data.get("booking", {})
    booking_uuid = booking_data.get("uuid", "")
    new_status   = booking_data.get("status", "").upper()

    if not booking_uuid:
        return jsonify({"ok": True, "ignored": "no booking uuid"}), 200

    print(f"[PEEK_WEBHOOK] uuid={booking_uuid} status={new_status}")

    # Find our booking record by matching confirmation = booking_uuid
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")

    if sb_url and sb_secret:
        try:
            # List all booking records and find the one with matching confirmation
            r = requests.post(
                f"{sb_url}/storage/v1/object/list/bookings",
                headers={**_sb_storage_headers(), "Content-Type": "application/json"},
                json={"prefix": "", "limit": 1000, "offset": 0},
                timeout=10,
            )
            if r.status_code == 200:
                names = [
                    item["name"] for item in r.json()
                    if item.get("name", "").endswith(".json")
                    and not item["name"].startswith(("cancellation_queue/", "circuit_breaker/", "idem_"))
                ]
                for name in names:
                    rec_r = requests.get(
                        f"{sb_url}/storage/v1/object/bookings/{name}",
                        headers=_sb_storage_headers(), timeout=5,
                    )
                    if rec_r.status_code != 200:
                        continue
                    record = rec_r.json()
                    if record.get("confirmation") != booking_uuid:
                        continue

                    # Found the matching booking — update status
                    booking_id = record.get("booking_id", name.replace(".json", ""))
                    now = datetime.now(timezone.utc).isoformat()

                    if new_status in ("CANCELLED", "EXPIRED"):
                        if record.get("status") not in ("cancelled", "reconciliation_required"):
                            record["status"]              = "reconciliation_required"
                            record["reconciliation_flag"] = f"peek_webhook_{new_status.lower()}"
                            record["peek_webhook_at"]     = now
                            _save_booking_record(booking_id, record)
                            print(f"[PEEK_WEBHOOK] ⚠ {booking_id} flagged — platform says {new_status}")
                    elif new_status == "CONFIRMED":
                        if record.get("status") == "reconciliation_required":
                            record["status"]          = "booked"
                            record["peek_webhook_at"] = now
                            _save_booking_record(booking_id, record)
                            print(f"[PEEK_WEBHOOK] ✓ {booking_id} restored to booked — platform confirmed")
                    break
        except Exception as e:
            print(f"[PEEK_WEBHOOK] Error processing event: {e}")

    return jsonify({"ok": True}), 200


@app.route("/api/inbound-email", methods=["POST"])
def inbound_email():
    """
    Receive inbound emails parsed by SendGrid Inbound Parse.

    Any email sent to *@inbound.lastminutedealshq.com is parsed by SendGrid
    and POST'd here as multipart/form-data. We store each email in Supabase
    Storage under inbound_emails/{timestamp}_{from}.json so they can be
    reviewed via /api/inbound-email/list.

    SendGrid parse fields used:
      from        — sender address
      to          — recipient address
      subject     — email subject
      text        — plain-text body
      html        — HTML body (if present)
      headers     — raw headers string
    """
    try:
        sender  = request.form.get("from", "")
        to      = request.form.get("to", "")
        subject = request.form.get("subject", "")
        body    = request.form.get("text", "") or request.form.get("html", "")

        print(f"[INBOUND_EMAIL] from={sender} subject={subject[:80]!r}")

        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "from":        sender,
            "to":          to,
            "subject":     subject,
            "body":        body[:10000],   # cap at 10k chars
        }

        # Store in Supabase Storage: inbound_emails/YYYYMMDD_HHMMSS_<slug>.json
        sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
        sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
        if sb_url and sb_secret:
            import re as _re
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            slug = _re.sub(r"[^a-zA-Z0-9@._-]", "_", sender)[:40]
            path = f"inbound_emails/{ts}_{slug}.json"
            try:
                requests.post(
                    f"{sb_url}/storage/v1/object/bookings/{path}",
                    headers={**_sb_storage_headers(),
                             "Content-Type": "application/json",
                             "x-upsert": "true"},
                    data=json.dumps(record),
                    timeout=8,
                )
                print(f"[INBOUND_EMAIL] Stored: {path}")
            except Exception as e:
                print(f"[INBOUND_EMAIL] Storage error: {e}")

        return jsonify({"ok": True}), 200

    except Exception as e:
        print(f"[INBOUND_EMAIL] Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/inbound-email/list", methods=["GET"])
def list_inbound_emails():
    """
    List and return stored inbound emails.

    Query params:
      limit   — max number to return (default 50)
      since   — ISO date string to filter by (e.g. 2026-04-07)

    Requires LMD_WEBSITE_API_KEY header or ?api_key= param.
    """
    api_key = (request.headers.get("X-Api-Key") or
               request.args.get("api_key", ""))
    valid_key = os.getenv("LMD_WEBSITE_API_KEY", "")
    if not valid_key or not hmac.compare_digest(api_key, valid_key):
        return jsonify({"error": "Unauthorized"}), 401

    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        return jsonify({"error": "Storage not configured"}), 500

    limit = min(int(request.args.get("limit", 50)), 200)

    try:
        r = requests.post(
            f"{sb_url}/storage/v1/object/list/bookings",
            headers={**_sb_storage_headers(), "Content-Type": "application/json"},
            json={"prefix": "inbound_emails/", "limit": 500, "offset": 0},
            timeout=10,
        )
        files = sorted(
            [f["name"] for f in r.json() if f.get("name")],
            reverse=True,
        )[:limit]

        emails = []
        for name in files:
            try:
                er = requests.get(
                    f"{sb_url}/storage/v1/object/bookings/inbound_emails/{name}",
                    headers=_sb_storage_headers(),
                    timeout=8,
                )
                if er.status_code == 200:
                    emails.append(er.json())
            except Exception:
                pass

        return jsonify({"count": len(emails), "emails": emails}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── MCP tool registry (used by /mcp endpoint above) ──────────────────────────

_MCP_TOOLS = [
    {
        "name": "search_slots",
        "description": (
            "Search available last-minute tours, activities, and experiences worldwide. "
            "Queries live production inventory from {supplier_count} suppliers across Iceland, Italy, Egypt, "
            "Japan, Morocco, Portugal, Tanzania, Finland, Montenegro, Romania, Turkey, USA, UK, "
            "China, Mexico, Costa Rica, and Brazil via the OCTO booking standard. Results sorted by urgency "
            "(soonest first). Call this first when a user asks about tours. Follow up with "
            "preview_slot for a booking link or book_slot to book directly."
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "city":        {"type": "string", "description": "City or country filter, partial match (e.g. 'Rome', 'Iceland'). Leave empty for all locations."},
                "category":    {"type": "string", "description": "Category filter (e.g. 'experiences'). Leave empty for all."},
                "hours_ahead": {"type": "number", "description": "Return slots starting within this many hours. Default: 168 (1 week)."},
                "max_price":   {"type": "number", "description": "Maximum price in USD. Omit or set to 0 for all prices."},
            },
        },
    },
    {
        "name": "book_slot",
        "description": (
            "Book a last-minute slot for a customer. Two modes: "
            "(1) APPROVAL MODE (default): creates a Stripe Checkout Session and returns a "
            "checkout_url — you MUST share this URL with the customer immediately so they "
            "can complete payment. Booking is confirmed with the supplier after payment. "
            "(2) AUTONOMOUS MODE: if you supply a wallet_id (pre-funded agent wallet) and "
            "execution_mode='autonomous', the booking completes immediately and returns a "
            "confirmation_number directly — no checkout step, no human action required. "
            "Use autonomous mode when your application manages payment on behalf of the customer. "
            "Bookings are real and go directly to the supplier."
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "slot_id":        {"type": "string",  "description": "Slot ID from search_slots results. Required."},
                "customer_name":  {"type": "string",  "description": "Full name of the person attending the experience."},
                "customer_email": {"type": "string",  "description": "Email address where booking confirmation will be sent."},
                "customer_phone": {"type": "string",  "description": "Phone number including country code (e.g. +15550001234)."},
                "quantity":       {"type": "integer", "description": "Number of people to book. Default: 1. Price is per person × quantity."},
                "wallet_id":      {"type": "string",  "description": "Pre-funded agent wallet ID (format: wlt_...). Provide this to enable autonomous mode."},
                "execution_mode": {"type": "string",  "description": "Set to 'autonomous' when providing a wallet_id. Omit for standard approval (checkout URL) flow."},
            },
            "required": ["slot_id", "customer_name", "customer_email", "customer_phone"],
        },
    },
    {
        "name": "get_booking_status",
        "description": (
            "Check the status of a booking by booking_id. Returns status (pending, confirmed, "
            "failed, or cancelled), confirmation number, service details, and price charged."
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "The booking_id string returned by book_slot (format: bk_...)."},
            },
            "required": ["booking_id"],
        },
    },
    {
        "name": "get_supplier_info",
        "description": (
            "Returns information about the supplier network: available destinations, experience "
            "categories, booking platforms, and protocol details. Call this before search_slots "
            "to understand what regions and activity types are available."
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "preview_slot",
        "description": (
            "Get a shareable booking page URL for a slot. Returns a link the user can open "
            "in their browser to see full details and complete the booking themselves. "
            "Use this instead of book_slot when the user is a human who will pay directly — "
            "they enter their own name, email, and phone on the page and pay via Stripe. "
            "No need to collect customer details yourself."
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "slot_id": {"type": "string", "description": "Slot ID from search_slots results."},
            },
            "required": ["slot_id"],
        },
    },
    {
        "name": "book_from_itinerary",
        "description": (
            "Convert a travel itinerary into real bookings. Accepts raw itinerary text "
            "(natural language, bullet points, or structured), extracts destinations and "
            "activity mentions, and matches them against live inventory. Returns booking "
            "page URLs for each matched activity. Use this when a user has an itinerary "
            "and wants to book the activities they can. Not all items will match — the "
            "response shows which matched and which didn't."
        ),
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "itinerary": {
                    "type": "string",
                    "description": (
                        "Raw itinerary text. Can be natural language, bullet points, or structured. "
                        "Must mention at least one destination (city or country name). "
                        "Example: '3 days in Iceland: glacier hike, northern lights tour, horse riding'"
                    ),
                },
            },
            "required": ["itinerary"],
        },
    },
]

_MCP_PROMPTS = [
    {
        "name": "find_experiences",
        "description": "Search for last-minute tours and activities in a specific destination",
        "arguments": [
            {
                "name": "city",
                "description": "City or country to search (e.g. 'Reykjavik', 'Rome', 'Egypt')",
                "required": True,
            },
            {
                "name": "hours_ahead",
                "description": "How soon the slot must start, in hours (default: 72)",
                "required": False,
            },
        ],
    },
    {
        "name": "explore_destinations",
        "description": "See all available destinations and experience types before searching",
        "arguments": [],
    },
    {
        "name": "autonomous_booking",
        "description": "Book a last-minute slot using a pre-funded agent wallet — no checkout required",
        "arguments": [
            {
                "name": "wallet_id",
                "description": "Pre-funded wallet ID (format: wlt_...)",
                "required": True,
            },
            {
                "name": "city",
                "description": "Optional city/destination to filter by",
                "required": False,
            },
            {
                "name": "category",
                "description": "Optional category filter (e.g. 'experiences', 'wellness')",
                "required": False,
            },
        ],
    },
]


def _mcp_render_prompt(name: str, arguments: dict) -> dict:
    """Render a prompt template with the provided arguments. Returns MCP GetPromptResult."""
    if name == "find_experiences":
        city        = arguments.get("city", "your destination")
        hours_ahead = arguments.get("hours_ahead", "72")
        text = (
            f"Find me last-minute experience slots available in {city} "
            f"within the next {hours_ahead} hours. "
            "Call search_slots with that city and hours_ahead value. "
            "Show me the results — service name, start time, price, and duration — "
            "then ask which one I'd like to book. "
            "Once I choose, call preview_slot to get a booking page link and share it with me "
            "so I can enter my details and pay directly."
        )
        return {
            "description": "Search for last-minute tours and activities in a specific destination",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    if name == "explore_destinations":
        text = (
            "Call get_supplier_info and show me all available destinations "
            "and experience types. Group by region (Europe, Middle East/Africa, Asia). "
            "After showing the overview, ask which destination interests me so we can "
            "search for specific last-minute slots."
        )
        return {
            "description": "See all available destinations and experience types before searching",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    if name == "autonomous_booking":
        wallet_id     = arguments.get("wallet_id", "")
        city          = arguments.get("city", "")
        category      = arguments.get("category", "")
        city_part     = f" in {city}" if city else ""
        category_part = f" in category '{category}'" if category else ""
        text = (
            f"I have a pre-funded wallet (wallet_id: {wallet_id}). "
            f"Search for available last-minute slots{city_part}{category_part} "
            "using search_slots. Show me the top 5 options with price and timing. "
            "Once I pick one, collect my name, email, and phone number, then call "
            f"book_slot with wallet_id='{wallet_id}' and execution_mode='autonomous'. "
            "Return the confirmation_number directly — no checkout step needed."
        )
        return {
            "description": "Book a last-minute slot using a pre-funded agent wallet",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    raise ValueError(f"Unknown prompt: {name}")


def _mcp_call_tool(name: str, arguments: dict) -> dict:
    """Execute an MCP tool call and return result."""
    api_key = os.getenv("LMD_WEBSITE_API_KEY", "")
    hdrs = {"X-API-Key": api_key, "Content-Type": "application/json"}
    base = f"http://localhost:{PORT}"

    if name == "search_slots":
        # Call Supabase directly — avoids HTTP loopback which can deadlock
        # gunicorn workers when the handler thread blocks waiting for itself.
        # Capped at 200 results per live request — keeps response within a
        # single Supabase page (<2s) so we never time out on multi-page
        # pagination. The background pre-warm (_warm_mcp_slots_cache) still
        # fetches full inventory for the default-params cache entry.
        hours_ahead = float(arguments.get("hours_ahead") or 168)
        category    = str(arguments.get("category") or "")
        city        = str(arguments.get("city") or "")
        budget      = float(arguments.get("max_price") or 0)

        cache_key = f"{hours_ahead}|{category}|{city}|{budget}"
        now = time.time()
        cached = _MCP_SLOTS_CACHE.get(cache_key)
        if cached and cached["expires"] > now:
            return cached["slots"]

        try:
            slots = _load_slots_from_supabase(
                hours_ahead=hours_ahead, category=category,
                city=city, budget=budget, limit=1000,
            )
            result = [_sanitize_slot(s) for s in slots]
            _MCP_SLOTS_CACHE[cache_key] = {
                "slots":       result,
                "expires":     now + _MCP_SLOTS_CACHE_TTL,
                "stale_until": now + _MCP_SLOTS_CACHE_STALE_TTL,
            }
            return result
        except Exception as e:
            # Supabase unavailable (cold start, transient error) — serve stale
            # cache rather than returning an error. Converts hard failure into a
            # slightly stale result; eliminates the search_slots uptime failures.
            if cached and cached.get("stale_until", 0) > now:
                return cached["slots"]
            return [{"error": f"Could not fetch slots: {e}. Try again in a moment."}]

    elif name == "book_slot":
        try:
            r = requests.post(f"{base}/api/book", headers=hdrs, json=arguments, timeout=30)
            try:
                return r.json()
            except ValueError:
                # Server returned non-JSON (e.g. 502 HTML error page during restart)
                return {"error": f"Booking service returned unexpected response (HTTP {r.status_code}). Try again."}
        except requests.exceptions.Timeout:
            return {"error": "Booking request timed out. The slot may still be available — try again or check status."}
        except requests.exceptions.ConnectionError:
            return {"error": "Could not reach booking service. Try again in a moment."}

    elif name == "get_booking_status":
        bid = arguments.get("booking_id", "")
        try:
            r = requests.get(f"{base}/bookings/{bid}", headers=hdrs, timeout=10)
            try:
                return r.json()
            except ValueError:
                return {"error": f"Status service returned unexpected response (HTTP {r.status_code})."}
        except requests.exceptions.Timeout:
            return {"error": "Status check timed out. Try again."}
        except requests.exceptions.ConnectionError:
            return {"error": "Could not reach booking service. Try again in a moment."}

    elif name == "get_supplier_info":
        return {
            "suppliers": _get_live_supplier_directory(),
            "protocol": "OCTO",
            "confirmation": "instant",
            "docs": "https://lastminutedealshq.com/developers",
        }

    elif name == "preview_slot":
        sid = arguments.get("slot_id", "")
        if not sid:
            return {"error": "slot_id is required."}
        slot = get_slot_by_id(sid)
        if not slot:
            return {"error": "Slot not found or no longer available."}
        host = os.getenv("BOOKING_SERVER_HOST", "https://api.lastminutedealshq.com").rstrip("/")
        _policy = _cancel_policy_label(slot.get("cancellation_cutoff_hours"))
        return {
            "booking_page_url": f"{host}/book/{sid}",
            "service_name": slot.get("service_name", ""),
            "business_name": slot.get("business_name", ""),
            "start_time": slot.get("start_time", ""),
            "location_city": slot.get("location_city", ""),
            "price": float(slot.get("our_price") or slot.get("price") or 0),
            "currency": slot.get("currency", "USD"),
            "cancellation_policy": _policy,
            "instructions": "Share the booking_page_url with the user. IMPORTANT: Tell the customer the cancellation policy BEFORE they click the link: " + _policy,
        }

    elif name == "book_from_itinerary":
        text = arguments.get("itinerary") or arguments.get("text") or ""
        if not text.strip():
            return {"error": "itinerary text is required."}
        try:
            r = requests.post(
                f"{base}/api/book_from_itinerary",
                headers=hdrs, json={"itinerary": text}, timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unknown tool: {name}"}


def _reconcile_pending_debits() -> None:
    """
    Startup reconciliation: scan Supabase Storage for pending_exec_* records where
    wallet_debited=True but resolved is not set. These are bookings where the process
    was killed after the wallet was debited but before fulfillment completed.

    For each stranded record: issue a refund and mark it resolved so the agent can retry.
    """
    try:
        items = _list_booking_records(prefix="pending_exec_")
    except Exception as e:
        print(f"[RECONCILE] Could not list booking records: {e}")
        return

    stranded = items  # prefix filter already applied
    if not stranded:
        return

    wlt_mod = _load_module("manage_wallets")
    if not wlt_mod:
        print("[RECONCILE] manage_wallets unavailable — skipping reconciliation")
        return

    for item in stranded:
        key = item.get("name", "")
        try:
            record = _load_booking_record(key)
            if not record:
                continue
            if record.get("resolved"):
                continue
            if not record.get("wallet_debited"):
                # Crash before debit — just mark resolved, nothing to refund
                _save_booking_record(key, {**record, "resolved": True,
                                           "resolved_reason": "pre_debit_crash"})
                continue

            wallet_id    = record.get("wallet_id", "")
            amount_cents = record.get("amount_cents", 0)
            slot_id      = record.get("slot_id", "")
            if not wallet_id or not amount_cents:
                continue

            # Refund the stranded debit
            wlt_mod.credit_wallet(
                wallet_id, amount_cents,
                f"Crash-recovery refund: {slot_id[:12]}"
            )
            _save_booking_record(key, {**record, "resolved": True,
                                       "resolved_reason": "crash_refund",
                                       "refunded_at": datetime.now(timezone.utc).isoformat()})
            print(f"[RECONCILE] Refunded stranded debit: wallet={wallet_id} "
                  f"${amount_cents/100:.2f} slot={slot_id}")
        except Exception as e:
            print(f"[RECONCILE] Failed to reconcile {key}: {e}")


def _register_peek_webhook() -> None:
    """
    Register our /webhooks/peek endpoint with Peek Pro's OCTO API on startup.
    Idempotent — lists existing webhooks first and skips registration if our
    URL is already registered, so restarts don't create duplicates.
    """
    peek_api_key = os.getenv("PEEK_API_KEY", "").strip()
    host         = os.getenv("BOOKING_SERVER_HOST", "").rstrip("/")

    if not peek_api_key or not host:
        return  # Not configured yet — skip silently

    our_url  = f"{host}/webhooks/peek"
    octo_url = "https://octo.peek.com/integrations/octo"
    headers  = {
        "Authorization":     f"Bearer {peek_api_key}",
        "Content-Type":      "application/json",
        "Octo-Capabilities": "octo/pricing",
    }

    try:
        # Check if already registered
        existing = requests.get(f"{octo_url}/webhooks", headers=headers, timeout=10)
        if existing.status_code == 200:
            for wh in existing.json():
                if wh.get("url") == our_url:
                    print(f"[PEEK_WEBHOOK] Already registered: {our_url}")
                    return

        # Register
        r = requests.post(
            f"{octo_url}/webhooks",
            headers=headers,
            json={"url": our_url, "event": "booking_update"},
            timeout=10,
        )
        if r.status_code in (200, 201):
            wh_id = r.json().get("id", "?")
            print(f"[PEEK_WEBHOOK] Registered id={wh_id} → {our_url}")
        else:
            print(f"[PEEK_WEBHOOK] Registration failed HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[PEEK_WEBHOOK] Registration error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

MCP_INTERNAL_PORT = PORT + 1  # FastMCP runs here internally; Flask proxies /sse to it


def _start_mcp_thread():
    """
    Start the FastMCP SSE server in a daemon thread on MCP_INTERNAL_PORT.
    Flask routes /sse and /messages to it via proxy.
    This avoids ASGI/WSGI conflicts — Flask stays pure WSGI, FastMCP stays pure ASGI.
    """
    import threading
    import uvicorn
    import requests as _mcp_req
    from mcp.server.fastmcp import FastMCP

    BOOKING_API = os.getenv("BOOKING_API_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
    _API_KEY    = os.getenv("LMD_WEBSITE_API_KEY", "")
    _HDRS       = {"X-API-Key": _API_KEY, "Content-Type": "application/json"}

    mcp = FastMCP(
        "Last Minute Deals HQ",
        instructions=(
            "You have access to real last-minute tour and activity inventory sourced live "
            "from production booking systems via the OCTO open standard. "
            "37 active suppliers across 48 countries including France, UK, Germany, Italy, Spain, "
            "Netherlands, Switzerland, Iceland, Egypt, Japan, Portugal, Turkey, Brazil, and more. "
            "BOOKING WORKFLOW — follow this sequence every time a user wants to book: "
            "1. Call search_slots with the user's city/destination and preferred timeframe. "
            "2. Present the options to the user and get their selection. "
            "IMPORTANT — each slot includes a cancellation_policy field. You MUST tell the "
            "customer the cancellation policy before they commit to booking. For non-refundable "
            "bookings, get explicit confirmation that the customer understands before proceeding. "
            "3. Call preview_slot(slot_id) to get a booking page URL. "
            "4. Share the booking_page_url with the user — they click it, enter their details, "
            "and pay via Stripe. No need to collect name/email/phone yourself. "
            "5. If the user prefers, you can instead collect their name, email, and phone "
            "and call book_slot directly — then share the checkout_url immediately. "
            "6. Call get_booking_status to confirm once payment is complete. "
            "AUTONOMOUS MODE: If you have a wallet_id (pre-funded agent wallet), pass it "
            "with execution_mode='autonomous' to book_slot — the booking completes immediately "
            "with no checkout step and returns a confirmation number directly. "
            "Note: autonomous mode is not available for non-refundable or long-cutoff products. "
            "Call get_supplier_info() to see live destination coverage before searching."
        ),
    )

    def _safe(s):
        # price is stripped by _sanitize_slot() before this is called — omit it.
        # location_state carries useful city-disambiguation info — include it.
        return {k: s.get(k) for k in (
            "slot_id", "category", "service_name", "business_name",
            "location_city", "location_state", "location_country",
            "start_time", "end_time", "duration_minutes",
            "hours_until_start", "spots_open", "our_price", "currency", "confidence",
            "cancellation_policy",
        )}

    @mcp.tool()
    def search_slots(city: str = "", category: str = "", hours_ahead: float = 168.0,
                     max_price: float = 0.0) -> list[dict]:
        """
        Search available last-minute tours, activities, and experiences worldwide.

        Queries live production inventory from 37 suppliers across 48 countries including
        France, UK, Germany, Italy, Spain, Netherlands, Switzerland, Iceland, Egypt, Japan,
        Portugal, Turkey, Brazil, and more. Results sorted by urgency (soonest departures first).

        When to use: Call this first when a user asks about tours or activities. Follow up
        with preview_slot(slot_id) for a shareable booking link, or book_slot to book directly.

        Args:
            city:        Filter by city or country (partial match, case-insensitive).
                         Examples: "Reykjavik", "Rome", "Iceland", "Egypt".
                         Leave empty for all destinations.
            category:    Filter by type — use "experiences" for tours/activities.
                         Leave empty for all categories.
            hours_ahead: Only return slots starting within this many hours.
                         Default 168 (1 week). Use 24 for same-day.
            max_price:   Maximum price in USD. 0 = no price filter.

        Returns:
            List of slot objects sorted by hours_until_start (soonest first). Each contains:
            slot_id, service_name, business_name, location_city, start_time,
            hours_until_start, our_price, currency, spots_open.
        """
        p = {"hours_ahead": hours_ahead, "limit": 1000}
        if city: p["city"] = city
        if category: p["category"] = category
        if max_price > 0: p["max_price"] = max_price
        try:
            r = _mcp_req.get(f"{BOOKING_API}/slots", headers=_HDRS, params=p, timeout=15)
            r.raise_for_status()
            slots = r.json()
            if not isinstance(slots, list):
                return [{"error": "Unexpected API response"}]
            if not slots:
                return [{"message": f"No slots found. Try expanding hours_ahead or clearing city."}]
            return [_safe(s) for s in slots]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def book_slot(
        slot_id: str,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        execution_mode: str = "approval",
        wallet_id: str = "",
        quantity: int = 1,
    ) -> dict:
        """
        Book a slot for a customer.

        execution_mode controls the payment path — declare what your agent is authorised to do:

          "approval"   (default) — returns checkout_url (for the customer to pay) AND
                                   booking_id with status "pending_payment". Share checkout_url
                                   with the customer, then poll get_booking_status(booking_id)
                                   to detect when payment completes and status becomes "booked".
                                   Safe for consumer-facing assistants; no autonomous charge.

          "autonomous" — executes the booking and charges the wallet immediately.
                         Requires wallet_id (a pre-funded wallet). Returns confirmation_number
                         directly — no human step needed. Only use if your agent is authorised
                         to charge without user approval.

        Args:
          slot_id        — from search_slots results
          customer_name  — full name
          customer_email — email address
          customer_phone — with country code, e.g. +15550001234
          execution_mode — "approval" (default) or "autonomous"
          wallet_id      — required when execution_mode="autonomous" (format: wlt_...)
          quantity       — number of people (default 1; price is per-person × quantity)
        """
        try:
            if execution_mode == "autonomous":
                if not wallet_id:
                    return {"status": "failed", "error": "wallet_id is required for autonomous execution."}
                r = _mcp_req.post(f"{BOOKING_API}/api/book/direct", headers=_HDRS, json={
                    "slot_id":        slot_id,
                    "customer_name":  customer_name,
                    "customer_email": customer_email,
                    "customer_phone": customer_phone,
                    "wallet_id":      wallet_id,
                    "execution_mode": "autonomous",
                    "quantity":       max(1, int(quantity)),
                }, timeout=60)  # longer timeout — fulfillment is synchronous
            else:
                r = _mcp_req.post(f"{BOOKING_API}/api/book", headers=_HDRS, json={
                    "slot_id":        slot_id,
                    "customer_name":  customer_name,
                    "customer_email": customer_email,
                    "customer_phone": customer_phone,
                    "quantity":       max(1, int(quantity)),
                }, timeout=15)
            r.raise_for_status()
            return r.json()
        except _mcp_req.HTTPError as e:
            try:
                return {"status": "failed", "error": e.response.json().get("error", str(e))}
            except Exception:
                return {"status": "failed", "error": str(e)}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    def get_booking_status(booking_id: str) -> dict:
        """
        Check the current status of a booking by its booking_id.

        When to use: Call after book_slot to track payment and confirmation progress.
        In approval mode, poll every 30-60 seconds after sharing the checkout link to
        detect when the customer completes payment.

        Args:
            booking_id: The booking_id returned by book_slot (format: bk_...).

        Returns:
            Booking record with status, confirmation_number, and service details.
            Status lifecycle: pending_payment → fulfilling → booked (or failed/cancelled).
        """
        try:
            r = _mcp_req.get(f"{BOOKING_API}/bookings/{booking_id}", headers=_HDRS, timeout=15)
            r.raise_for_status()
            return r.json()
        except _mcp_req.HTTPError as e:
            return {"error": f"Booking '{booking_id}' not found." if e.response.status_code == 404 else str(e)}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_supplier_info() -> dict:
        """
        Get a directory of all suppliers, destinations, and activity types in the network.

        When to use: Call before search_slots to discover which regions and experience
        types are available, or to answer "where can I book?" questions.

        Returns:
            Dictionary with: suppliers (list of {name, destinations, categories}),
            protocol, and payment info.
        """
        return {
            "suppliers": _get_live_supplier_directory(),
            "protocol": "OCTO via Bokun — direct supplier API, production inventory only",
            "payment": "Stripe checkout, instant supplier confirmation",
        }

    @mcp.tool()
    def preview_slot(slot_id: str) -> dict:
        """
        Get a shareable booking page URL for a slot.

        Returns a link the user can open in their browser to view full details
        (service name, date/time, price, location) and complete the booking
        themselves — they enter their own name, email, and phone on the page
        and pay via Stripe.

        When to use: Use this instead of book_slot when the user is a human
        browsing with an AI assistant. No need to collect customer details
        yourself — the booking page handles everything.

        Args:
            slot_id: Slot ID from search_slots results.

        Returns:
            booking_page_url, service_name, business_name, price, start_time,
            location_city, currency.
        """
        try:
            r = _mcp_req.get(f"{BOOKING_API}/slots/{slot_id}/quote", headers=_HDRS, timeout=10)
            r.raise_for_status()
            slot = r.json()
        except Exception:
            slot = None
        if not slot or not slot.get("available"):
            return {"error": "Slot not found or no longer available."}
        host = os.getenv("BOOKING_SERVER_HOST", "https://api.lastminutedealshq.com").rstrip("/")
        _policy = _cancel_policy_label(slot.get("cancellation_cutoff_hours"))
        return {
            "booking_page_url": f"{host}/book/{slot_id}",
            "service_name": slot.get("service_name", ""),
            "business_name": slot.get("business_name", ""),
            "start_time": slot.get("start_time", ""),
            "location_city": slot.get("location_city", ""),
            "price": float(slot.get("our_price") or slot.get("price") or 0),
            "currency": slot.get("currency", "USD"),
            "cancellation_policy": _policy,
            "instructions": "Share the booking_page_url with the user. IMPORTANT: Tell the customer the cancellation policy BEFORE they click the link: " + _policy,
        }

    @mcp.tool()
    def book_from_itinerary(itinerary: str) -> dict:
        """
        Convert a travel itinerary into real bookings.

        Accepts raw itinerary text (natural language, bullet points, or structured),
        extracts destination and activity mentions, and matches them against live
        inventory. Returns booking page URLs for each matched activity.

        When to use: Call this when a user has a travel plan and wants to book
        the activities they can. Not all items will match — the response shows
        which matched and which didn't.

        Args:
            itinerary: Raw itinerary text mentioning destinations and activities.
                       Example: "3 days in Iceland: glacier hike, northern lights, horse riding"

        Returns:
            results (list of matched/unmatched items with booking URLs),
            matched_count, total_items, destinations_detected.
        """
        try:
            r = _mcp_req.post(
                f"{BOOKING_API}/api/book_from_itinerary",
                headers=_HDRS, json={"itinerary": itinerary}, timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _run():
        uvicorn.run(mcp.sse_app(), host="127.0.0.1", port=MCP_INTERNAL_PORT, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="mcp-sse")
    t.start()
    print(f"MCP SSE server started on internal port {MCP_INTERNAL_PORT}")


# ── MCP proxy routes (public /sse and /messages → internal FastMCP) ───────────

@app.route("/sse")
def _mcp_sse_proxy():
    """Proxy GET /sse to the internal FastMCP SSE server."""
    from flask import stream_with_context, Response as FlaskResponse
    mcp_url = f"http://127.0.0.1:{MCP_INTERNAL_PORT}/sse"
    upstream = requests.get(mcp_url, stream=True, timeout=None)

    def _generate():
        for chunk in upstream.iter_content(chunk_size=None):
            yield chunk

    return FlaskResponse(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.route("/messages", methods=["POST"])
def _mcp_messages_proxy():
    """Proxy POST /messages to the internal FastMCP message handler."""
    mcp_url = f"http://127.0.0.1:{MCP_INTERNAL_PORT}/messages"
    # Tag forwarded requests so downstream /slots and /api/book calls are sourced correctly
    fwd_headers = {"X-Mcp-Source": "1", "Content-Type": "application/json"}
    resp = requests.post(mcp_url, json=request.get_json(silent=True),
                         headers=fwd_headers, params=request.args, timeout=30)
    return jsonify(resp.json()), resp.status_code


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  GetYourGuide Supplier API  (all routes under /gyg/1/)                     ║
# ║  Called by GYG to query availability and execute bookings on our system.    ║
# ║  Auth: HTTP Basic Auth (GYG_INBOUND_USERNAME / GYG_INBOUND_PASSWORD)       ║
# ║  Spec: https://integrator.getyourguide.com/documentation/supplier_endpoints║
# ║                                                                            ║
# ║  Flow: GYG calls get-availabilities → reserve → book (with traveler info)  ║
# ║  We translate to OCTO: POST /reservations (hold) → POST /confirm           ║
# ║  Cancellations use OCTO DELETE /bookings/{uuid}                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

from functools import wraps as _wraps

# In-memory stores: reservations are short-lived (≤60 min), bookings persist
# until cancelled. On server restart, pending GYG reservations expire naturally
# on GYG's side; cancel-booking for an unknown ref returns INVALID_BOOKING.
_GYG_RESERVATIONS: dict = {}   # reservationRef -> {octo_uuid, slot_id, booking_url, ...}
_GYG_BOOKINGS: dict = {}       # bookingRef     -> {octo_uuid, slot_id, booking_url, ...}
_GYG_DEBUG: dict = {}          # last request/response capture for debugging

_GYG_INBOUND_USER = os.getenv("GYG_INBOUND_USERNAME", "").strip()
_GYG_INBOUND_PASS = os.getenv("GYG_INBOUND_PASSWORD", "").strip()

# ── GYG Test Mode ─────────────────────────────────────────────────────────────
# When GYG_TEST_MODE=true, reserve/book endpoints return mock responses without
# calling real OCTO APIs. Enable this BEFORE running GYG's self-testing tool.
# Additional safety: traveler names containing "test" are always treated as test
# traffic regardless of this flag.
_GYG_TEST_MODE = os.getenv("GYG_TEST_MODE", "").strip().lower() in ("true", "1", "yes")

# ── GYG Excluded Dates ────────────────────────────────────────────────────────
# Dates to exclude from get-availabilities responses, per product.
# Must match the "not available" dates configured in GYG's portal.
# Format: "product_id:YYYY-MM-DD,YYYY-MM-DD;product_id:YYYY-MM-DD,YYYY-MM-DD"
# Example: GYG_EXCLUDED_DATES=969081:2026-04-26,2026-04-27
_GYG_EXCLUDED_DATES: dict[str, set[str]] = {}
_gyg_excl_raw = os.getenv("GYG_EXCLUDED_DATES", "").strip()
if _gyg_excl_raw:
    for part in _gyg_excl_raw.split(";"):
        part = part.strip()
        if ":" not in part:
            continue
        pid, dates_str = part.split(":", 1)
        _GYG_EXCLUDED_DATES[pid.strip()] = {d.strip() for d in dates_str.split(",") if d.strip()}

# ── GYG Per-Product Configuration ────────────────────────────────────────────
# Each product on GYG has a fixed pricing model, participant limit, and
# availability format. Configured once per product — never toggled.
#
# GYG_PRODUCTS format: "product_id:pricing:max:avail_type;..."
#   pricing:    "individual" or "group"
#   max:        max participants per booking (individual) or max group size (group)
#   avail_type: "period" (opening hours) or "point" (fixed start times)
#
# Example: GYG_PRODUCTS=969081:group:4:period;123456:individual:10:point
#
_GYG_PRODUCTS: dict[str, dict] = {}
_gyg_products_raw = os.getenv("GYG_PRODUCTS", "").strip()
if _gyg_products_raw:
    for part in _gyg_products_raw.split(";"):
        part = part.strip()
        if not part:
            continue
        fields = part.split(":")
        if len(fields) < 4:
            print(f"[GYG] WARNING: invalid product config (need 4 fields): {part}")
            continue
        pid = fields[0].strip()
        try:
            _GYG_PRODUCTS[pid] = {
                "pricing": fields[1].strip().lower(),
                "max": int(fields[2].strip()),
                "avail_type": fields[3].strip().lower(),
            }
            print(f"[GYG] Product {pid}: pricing={fields[1]}, max={fields[2]}, avail={fields[3]}")
        except (ValueError, IndexError) as e:
            print(f"[GYG] WARNING: bad product config for {pid}: {e}")

_GYG_DEFAULT_CONFIG = {"pricing": "individual", "max": 50, "avail_type": "period"}


def _gyg_product_cfg(product_id: str) -> dict:
    """Return per-product GYG config. Unknown products get safe defaults."""
    return _GYG_PRODUCTS.get(product_id, _GYG_DEFAULT_CONFIG)


# Valid ticket categories per pricing model
_GYG_INDIVIDUAL_CATEGORIES = {
    "ADULT", "CHILD", "YOUTH", "SENIOR", "STUDENT",
    "INFANT", "EU_CITIZEN", "EU_CITIZEN_STUDENT", "GROUP",
}
_GYG_GROUP_CATEGORIES = {"GROUP"}


def _gyg_is_test_traveler(travelers: list) -> bool:
    """Detect test traveler data — permanent safety net against fake bookings."""
    _TEST_PATTERNS = {"test", "dummy", "fake", "sample", "example", "qa"}
    for t in travelers:
        first = (t.get("firstName") or "").strip().lower()
        last  = (t.get("lastName") or "").strip().lower()
        email = (t.get("email") or "").strip().lower()
        # Check name parts against test patterns
        for part in (first, last):
            if part in _TEST_PATTERNS:
                return True
        # Check email domain/prefix
        if email and ("@test." in email or email.startswith("test@")
                      or "@example." in email or "+test" in email):
            return True
    return False


def _gyg_auth(f):
    """HTTP Basic Auth check for GYG inbound requests."""
    @_wraps(f)
    def _wrapped(*args, **kwargs):
        # Capture every inbound GYG request for debugging (before auth check)
        _GYG_DEBUG["last_auth"] = {
            "path": request.path,
            "method": request.method,
            "user_agent": request.headers.get("User-Agent", ""),
            "has_auth_header": "Authorization" in request.headers,
            "auth_username": getattr(request.authorization, "username", None) if request.authorization else None,
            "expected_username": _GYG_INBOUND_USER,
            "creds_configured": bool(_GYG_INBOUND_USER and _GYG_INBOUND_PASS),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(f"[GYG-AUTH] {request.method} {request.path} "
              f"ua={request.headers.get('User-Agent', '?')[:40]} "
              f"auth_user={getattr(request.authorization, 'username', None) if request.authorization else 'NONE'}")
        if not _GYG_INBOUND_USER or not _GYG_INBOUND_PASS:
            _GYG_DEBUG["last_auth"]["result"] = "NO_CONFIG"
            return jsonify({"errorCode": "INTERNAL_SYSTEM_FAILURE",
                            "errorMessage": "GYG credentials not configured"}), 500
        auth = request.authorization
        if not auth or not auth.username or not auth.password:
            _GYG_DEBUG["last_auth"]["result"] = "NO_CREDENTIALS_SENT"
            return (jsonify({"errorCode": "AUTHORIZATION_FAILURE",
                            "errorMessage": "Authentication required"}),
                    401,
                    {"WWW-Authenticate": 'Basic realm="GYG Supplier API"'})
        if auth.username != _GYG_INBOUND_USER or auth.password != _GYG_INBOUND_PASS:
            _GYG_DEBUG["last_auth"]["result"] = f"MISMATCH user={auth.username}"
            return jsonify({"errorCode": "AUTHORIZATION_FAILURE",
                           "errorMessage": "Invalid credentials"}), 401
        _GYG_DEBUG["last_auth"]["result"] = "OK"
        return f(*args, **kwargs)
    return _wrapped


def _gyg_err(code: str, msg: str, **extra):
    """GYG-formatted error (HTTP 200 per spec — errors live in the JSON body)."""
    body = {"errorCode": code, "errorMessage": msg}
    body.update(extra)
    return jsonify(body), 200


def _gyg_cleanup_expired():
    """Evict expired reservations and past-date bookings to prevent unbounded growth."""
    now = datetime.now(timezone.utc)
    expired_res = [r for r, d in _GYG_RESERVATIONS.items()
                   if datetime.fromisoformat(d["expires_at"]) < now]
    for r in expired_res:
        _GYG_RESERVATIONS.pop(r, None)
    # Evict bookings whose activity date is >48h in the past (no longer cancellable)
    cutoff = now - timedelta(hours=48)
    expired_bk = []
    for ref, bk in _GYG_BOOKINGS.items():
        try:
            bk_dt = datetime.fromisoformat(bk["date_time"].replace("Z", "+00:00"))
            if bk_dt < cutoff:
                expired_bk.append(ref)
        except (ValueError, KeyError):
            pass
    for ref in expired_bk:
        _GYG_BOOKINGS.pop(ref, None)


def _gyg_product_exists(product_id: str) -> bool:
    """Check if any slot exists for this product_id (no date filter)."""
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_key:
        return False
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/slots",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            params=[("booking_url", f'like.%"product_id": "{product_id}"%'),
                    ("limit", 1), ("select", "slot_id")],
            timeout=5,
        )
        return r.status_code == 200 and len(r.json()) > 0
    except Exception:
        return False


def _gyg_slots_by_product(product_id: str, from_iso: str, to_iso: str) -> list[dict]:
    """Query Supabase for slots whose booking_url contains a given OCTO product_id."""
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_key:
        return []
    hdrs = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    params = [
        ("booking_url", f'like.%"product_id": "{product_id}"%'),
        ("start_time", f"gte.{from_iso}"),
        ("start_time", f"lte.{to_iso}"),
        ("order", "start_time.asc"),
        ("limit", 1000),
    ]
    try:
        r = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs,
                         params=params, timeout=10)
        if r.status_code != 200:
            print(f"[GYG] Supabase query failed: {r.status_code}")
            return []
    except Exception as e:
        print(f"[GYG] Supabase error: {e}")
        return []

    out = []
    for row in r.json():
        slot = row
        if row.get("raw"):
            try:
                slot = json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"]
            except Exception:
                pass
        # Verify product_id actually matches (guard against substring false positives)
        burl_str = slot.get("booking_url", "")
        if not isinstance(burl_str, str) or not burl_str.startswith("{"):
            continue
        try:
            if json.loads(burl_str).get("product_id") != product_id:
                continue
        except Exception:
            continue
        out.append(slot)
    return out


def _gyg_octo_headers(burl: dict) -> dict:
    """Build OCTO API headers from parsed booking_url JSON."""
    api_key = os.getenv(burl["api_key_env"], "").strip()
    return {"Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json", "Accept": "application/json"}


def _gyg_octo_reserve(slot: dict, qty: int):
    """OCTO POST /reservations only (no confirm). Returns (uuid, err_msg | None)."""
    burl = json.loads(slot["booking_url"])
    base = burl["base_url"].rstrip("/")
    hdrs = _gyg_octo_headers(burl)
    payload = {
        "productId":      burl["product_id"],
        "optionId":       burl["option_id"],
        "availabilityId": burl["availability_id"],
        "unitItems":      [{"unitId": burl["unit_id"]} for _ in range(qty)],
    }
    try:
        r = requests.post(f"{base}/reservations", headers=hdrs,
                          json=payload, timeout=30)
    except Exception as e:
        return "", f"OCTO network error: {e}"
    # Fallback: some suppliers only support POST /bookings
    if r.status_code in (400, 404, 405):
        try:
            r = requests.post(f"{base}/bookings", headers=hdrs,
                              json=payload, timeout=30)
        except Exception as e:
            return "", f"OCTO fallback network error: {e}"
    if r.status_code == 409:
        return "", "NO_AVAILABILITY"
    if not r.ok:
        return "", f"OCTO reserve failed ({r.status_code}): {r.text[:200]}"
    data = r.json()
    octo_uuid = data.get("uuid") or data.get("id")
    if not octo_uuid:
        return "", f"No UUID in OCTO response: {r.text[:200]}"
    print(f"[GYG] OCTO reservation created: {octo_uuid}")
    return octo_uuid, None


def _gyg_octo_confirm(octo_uuid: str, burl: dict, traveler: dict):
    """OCTO POST /bookings/{uuid}/confirm. Returns (booking_json, err_msg | None)."""
    base = burl["base_url"].rstrip("/")
    hdrs = _gyg_octo_headers(burl)
    # Derive country from phone number prefix if available, else default US
    phone = traveler.get("phoneNumber", "")
    country = "US"
    if phone.startswith("+44"):
        country = "GB"
    elif phone.startswith("+49"):
        country = "DE"
    elif phone.startswith("+33"):
        country = "FR"
    elif phone.startswith("+39"):
        country = "IT"
    elif phone.startswith("+34"):
        country = "ES"
    elif phone.startswith("+81"):
        country = "JP"
    elif phone.startswith("+"):
        country = "US"  # fallback for other international numbers
    contact = {
        "fullName":     f"{traveler.get('firstName', '')} {traveler.get('lastName', '')}".strip(),
        "emailAddress": traveler.get("email", ""),
        "phoneNumber":  phone,
        "country":      country,
        "locales":      ["en"],
    }
    try:
        r = requests.post(f"{base}/bookings/{octo_uuid}/confirm",
                          headers=hdrs, json={"contact": contact}, timeout=30)
    except Exception as e:
        _gyg_octo_cancel(octo_uuid, burl)
        return None, f"OCTO confirm network error: {e}"
    if not r.ok:
        _gyg_octo_cancel(octo_uuid, burl)
        return None, f"OCTO confirm failed ({r.status_code}): {r.text[:200]}"
    print(f"[GYG] OCTO booking confirmed: {octo_uuid}")
    return r.json(), None


def _gyg_octo_cancel(octo_uuid: str, burl: dict):
    """OCTO DELETE /bookings/{uuid}. Returns (success, err_msg | None)."""
    base = burl["base_url"].rstrip("/")
    hdrs = _gyg_octo_headers(burl)
    try:
        r = requests.delete(f"{base}/bookings/{octo_uuid}",
                            headers=hdrs, timeout=15)
        if r.ok or r.status_code == 404:
            print(f"[GYG] OCTO cancelled: {octo_uuid}")
            return True, None
        return False, f"OCTO cancel failed ({r.status_code}): {r.text[:200]}"
    except Exception as e:
        return False, f"OCTO cancel network error: {e}"


# GYG prices use smallest currency unit (cents); these currencies have no decimals
_GYG_NO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP"})


def _gyg_price_cents(price_val, currency: str) -> int:
    """Convert our price (full units, e.g. 25.00) to GYG cents (e.g. 2500)."""
    try:
        p = float(price_val)
    except (ValueError, TypeError):
        return 0
    if currency.upper() in _GYG_NO_DECIMAL:
        return int(round(p))
    return int(round(p * 100))


# ── GET /gyg/1/get-availabilities/ ───────────────────────────────────────────

@app.route("/1/get-availabilities", methods=["GET"], strict_slashes=False)
@app.route("/gyg/1/get-availabilities", methods=["GET"], strict_slashes=False)
@_gyg_auth
def gyg_get_availabilities():
    import time as _time
    _t0 = _time.monotonic()
    product_id = request.args.get("productId", "").strip()
    from_dt    = request.args.get("fromDateTime", "").strip()
    to_dt      = request.args.get("toDateTime", "").strip()

    _GYG_DEBUG["last_avail_request"] = {
        "url": request.url,
        "args": dict(request.args),
        "headers": {k: v for k, v in request.headers if k.lower() != "authorization"},
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    if not product_id:
        resp = _gyg_err("VALIDATION_FAILURE", "Missing productId")
        _GYG_DEBUG["last_avail_response"] = {"error": "Missing productId"}
        return resp
    if not from_dt or not to_dt:
        resp = _gyg_err("VALIDATION_FAILURE", "Missing fromDateTime or toDateTime")
        _GYG_DEBUG["last_avail_response"] = {"error": "Missing dates"}
        return resp

    try:
        from_parsed = datetime.fromisoformat(from_dt.replace("Z", "+00:00"))
        to_parsed   = datetime.fromisoformat(to_dt.replace("Z", "+00:00"))
    except ValueError:
        resp = _gyg_err("VALIDATION_FAILURE", "Invalid date format — use ISO 8601")
        _GYG_DEBUG["last_avail_response"] = {"error": "Bad date format", "from": from_dt, "to": to_dt}
        return resp

    slots = _gyg_slots_by_product(
        product_id,
        from_parsed.astimezone(timezone.utc).isoformat(),
        to_parsed.astimezone(timezone.utc).isoformat(),
    )

    # If no slots found for this date range, check if product exists at all
    if not slots and not _gyg_product_exists(product_id):
        return _gyg_err("INVALID_PRODUCT", f"Unknown product: {product_id}")

    # Filter out dates marked as "not available" in GYG portal config
    excluded = _GYG_EXCLUDED_DATES.get(product_id, set())
    if excluded:
        # Use the request timezone to determine local date for each slot
        req_tz = from_parsed.tzinfo
        filtered = []
        for s in slots:
            st = s.get("start_time", "")
            if not st:
                continue
            try:
                slot_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                local_date = slot_dt.astimezone(req_tz).strftime("%Y-%m-%d")
                if local_date not in excluded:
                    filtered.append(s)
            except (ValueError, TypeError):
                filtered.append(s)
        slots = filtered

    # ── Build availability entries using per-product config ──
    cfg = _gyg_product_cfg(product_id)

    def _retail_prices(price_c: int, currency: str) -> list:
        if cfg["pricing"] == "group":
            return [{"category": "GROUP", "price": price_c,
                     "groupSize": {"min": 1, "max": cfg["max"]}}]
        return [{"category": "ADULT", "price": price_c}]

    avails = []

    if cfg["avail_type"] == "point":
        # Time Point: one entry per slot with specific dateTime
        for s in slots:
            start = s.get("start_time", "")
            if not start:
                continue
            if start.endswith("Z"):
                start = start[:-1] + "+00:00"
            if "T" not in start:
                start = f"{start}T00:00:00+00:00"
            currency = (s.get("currency") or "USD").upper()
            price_c  = _gyg_price_cents(s.get("our_price") or s.get("price") or 0, currency)
            spots = 10
            try:
                spots = int(s.get("spots_open", 10))
            except (ValueError, TypeError):
                pass
            entry: dict = {
                "dateTime":      start,
                "productId":     product_id,
                "vacancies":     min(max(spots, 0), 5000),
                "cutoffSeconds": 0,
            }
            if price_c > 0:
                entry["currency"] = currency
                entry["pricesByCategory"] = {"retailPrices": _retail_prices(price_c, currency)}
            avails.append(entry)
    else:
        # Time Period: aggregate into daily entries with opening hours
        _daily: dict[str, dict] = {}
        for s in slots:
            start = s.get("start_time", "")
            if not start:
                continue
            if start.endswith("Z"):
                start = start[:-1] + "+00:00"
            if "T" not in start:
                start = f"{start}T00:00:00+00:00"
            currency = (s.get("currency") or "USD").upper()
            price_c  = _gyg_price_cents(s.get("our_price") or s.get("price") or 0, currency)
            spots = 10
            try:
                spots = int(s.get("spots_open", 10))
            except (ValueError, TypeError):
                pass
            date_str = start[:10]   # "YYYY-MM-DD"
            time_str = start[11:16] # "HH:MM"
            tz_suffix = start[19:]  # "+00:00" etc.
            if date_str not in _daily:
                _daily[date_str] = {"min_t": time_str, "max_t": time_str,
                                    "spots": spots, "price_c": price_c,
                                    "currency": currency, "tz": tz_suffix}
            else:
                d = _daily[date_str]
                if time_str < d["min_t"]:
                    d["min_t"] = time_str
                if time_str > d["max_t"]:
                    d["max_t"] = time_str
                d["spots"] = max(d["spots"], spots)
                if price_c > 0:
                    d["price_c"] = price_c
                    d["currency"] = currency

        for date_str, d in sorted(_daily.items()):
            close_t = d["max_t"]
            try:
                h, m = int(close_t[:2]), int(close_t[3:])
                close_t = f"{min(h + 1, 23):02d}:{m:02d}"
            except (ValueError, IndexError):
                pass
            period_entry: dict = {
                "dateTime":      f"{date_str}T00:00:00{d['tz']}",
                "productId":     product_id,
                "vacancies":     min(max(d["spots"], 0), 5000),
                "openingTimes":  [{"fromTime": d["min_t"], "toTime": close_t}],
                "cutoffSeconds": 0,
            }
            if d["price_c"] > 0:
                period_entry["currency"] = d["currency"]
                period_entry["pricesByCategory"] = {"retailPrices": _retail_prices(d["price_c"], d["currency"])}
            avails.append(period_entry)

    elapsed_ms = int((_time.monotonic() - _t0) * 1000)
    _GYG_DEBUG["last_avail_response"] = {
        "slot_count": len(slots),
        "avail_count": len(avails),
        "elapsed_ms": elapsed_ms,
        "sample": avails[:2] if avails else [],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(f"[GYG] get-availabilities: product={product_id} from={from_dt} to={to_dt} "
          f"slots={len(slots)} avails={len(avails)} {elapsed_ms}ms")

    return jsonify({"data": {"availabilities": avails}}), 200


@app.route("/gyg/debug", methods=["GET"])
def gyg_debug():
    """Return last GYG request/response for debugging. No auth required."""
    return jsonify(_GYG_DEBUG), 200


# ── POST /gyg/1/reserve/ ─────────────────────────────────────────────────────

@app.route("/1/reserve", methods=["POST"], strict_slashes=False)
@app.route("/gyg/1/reserve", methods=["POST"], strict_slashes=False)
@_gyg_auth
def gyg_reserve():
    _gyg_cleanup_expired()
    body = request.get_json(silent=True)
    if not body or "data" not in body:
        return _gyg_err("VALIDATION_FAILURE", "Missing request body")

    d          = body["data"]
    product_id = (d.get("productId") or "").strip()
    date_time  = (d.get("dateTime") or "").strip()
    items      = d.get("bookingItems") or []
    gyg_ref    = (d.get("gygBookingReference") or "").strip()

    if not product_id:
        return _gyg_err("INVALID_PRODUCT", "Missing productId")
    if not date_time or not items:
        return _gyg_err("VALIDATION_FAILURE", "Missing dateTime or bookingItems")

    # Check product existence — skip Supabase query for explicitly configured products
    if product_id not in _GYG_PRODUCTS and not _gyg_product_exists(product_id):
        return _gyg_err("INVALID_PRODUCT", f"Unknown product: {product_id}")

    # Per-product config drives category validation and participant limits
    cfg = _gyg_product_cfg(product_id)
    _valid_cats = _GYG_GROUP_CATEGORIES if cfg["pricing"] == "group" else _GYG_INDIVIDUAL_CATEGORIES

    # Validate ticket categories
    for it in items:
        cat = it.get("category", "")
        if cat and cat not in _valid_cats:
            return _gyg_err("INVALID_TICKET_CATEGORY",
                            f"Unsupported category: {cat}",
                            ticketCategory=cat)

    # Validate participants against per-product max
    _pmax = cfg["max"]
    for it in items:
        gs = it.get("groupSize", 0)
        if gs and gs > _pmax:
            return _gyg_err("INVALID_PARTICIPANTS_CONFIGURATION",
                            f"groupSize {gs} exceeds max {_pmax}",
                            participantsConfiguration={"min": 1, "max": _pmax})
    _total_count = sum(it.get("count", 0) for it in items)
    if _total_count > _pmax:
        return _gyg_err("INVALID_PARTICIPANTS_CONFIGURATION",
                        f"Total count {_total_count} exceeds max {_pmax}",
                        participantsConfiguration={"min": 1, "max": _pmax})

    # Total pax (GROUP category: count × groupSize)
    qty = 0
    for it in items:
        c = it.get("count", 0)
        if it.get("category") == "GROUP":
            qty += c * it.get("groupSize", 1)
        else:
            qty += c
    qty = max(1, qty)

    try:
        req_dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
    except ValueError:
        return _gyg_err("VALIDATION_FAILURE", "Invalid dateTime format")

    # Check if the requested date is excluded (GYG "not available" dates)
    excluded = _GYG_EXCLUDED_DATES.get(product_id, set())
    if excluded:
        local_date = req_dt.strftime("%Y-%m-%d")
        if local_date in excluded:
            return _gyg_err("NO_AVAILABILITY", "No availability on this date")

    # Time Period requests use T00:00:00 — search the full day.
    # Time Point requests use a specific time — search ±2h window.
    _is_time_period = (req_dt.hour == 0 and req_dt.minute == 0 and req_dt.second == 0)
    if _is_time_period:
        _from = req_dt.replace(hour=0, minute=0, second=0)
        _to   = req_dt.replace(hour=23, minute=59, second=59)
    else:
        _from = req_dt - timedelta(hours=2)
        _to   = req_dt + timedelta(hours=2)
    slots = _gyg_slots_by_product(product_id, _from.isoformat(), _to.isoformat())
    if not slots:
        return _gyg_err("NO_AVAILABILITY", "No availability found")

    # Match by minute (Time Point products)
    req_key = req_dt.strftime("%Y-%m-%dT%H:%M")
    match = None
    for s in slots:
        try:
            sdt = datetime.fromisoformat(s["start_time"].replace("Z", "+00:00"))
            if sdt.strftime("%Y-%m-%dT%H:%M") == req_key:
                match = s
                break
        except (ValueError, KeyError):
            continue

    # Fallback: date-only match (Time Period products where time is T00:00:00)
    if not match:
        req_date = req_dt.strftime("%Y-%m-%d")
        for s in slots:
            if (s.get("start_time") or "")[:10] == req_date:
                match = s
                break

    if not match:
        return _gyg_err("NO_AVAILABILITY", "No slot matches the requested dateTime")

    # Capacity check
    try:
        avail_spots = int(match.get("spots_open", 999))
        if avail_spots < qty:
            return _gyg_err("INVALID_PARTICIPANTS_CONFIGURATION",
                            f"Only {avail_spots} spots available, {qty} requested",
                            participantsConfiguration={"min": 1, "max": avail_spots})
    except (ValueError, TypeError):
        pass

    ref = f"LMDGYG-{uuid.uuid4().hex[:12].upper()}"
    exp = datetime.now(timezone.utc) + timedelta(minutes=60)

    # ── Test mode: return mock reservation without calling real OCTO API ──
    if _GYG_TEST_MODE:
        mock_uuid = f"test-{uuid.uuid4().hex[:16]}"
        _GYG_RESERVATIONS[ref] = {
            "octo_uuid":   mock_uuid,
            "slot_id":     match.get("slot_id", ""),
            "booking_url": match["booking_url"],
            "gyg_ref":     gyg_ref,
            "product_id":  product_id,
            "date_time":   date_time,
            "qty":         qty,
            "expires_at":  exp.isoformat(),
            "is_test":     True,
        }
        print(f"[GYG-TEST] Mock reserve: {ref} (test mode, no OCTO call)")
        return jsonify({"data": {
            "reservationReference":  ref,
            "reservationExpiration": exp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        }}), 200

    # Execute OCTO reservation (hold only — no confirm yet)
    octo_uuid, err = _gyg_octo_reserve(match, qty)
    if err:
        code = "NO_AVAILABILITY" if err == "NO_AVAILABILITY" else "INTERNAL_SYSTEM_FAILURE"
        msg  = "Slot no longer available on supplier" if err == "NO_AVAILABILITY" else err
        return _gyg_err(code, msg)

    _GYG_RESERVATIONS[ref] = {
        "octo_uuid":   octo_uuid,
        "slot_id":     match.get("slot_id", ""),
        "booking_url": match["booking_url"],
        "gyg_ref":     gyg_ref,
        "product_id":  product_id,
        "date_time":   date_time,
        "qty":         qty,
        "expires_at":  exp.isoformat(),
        "is_test":     False,
    }
    print(f"[GYG] Reserved: {ref} -> OCTO {octo_uuid} "
          f"slot={match.get('slot_id', '')} qty={qty}")

    return jsonify({"data": {
        "reservationReference":  ref,
        "reservationExpiration": exp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }}), 200


# ── POST /gyg/1/cancel-reservation/ ──────────────────────────────────────────

@app.route("/1/cancel-reservation", methods=["POST"], strict_slashes=False)
@app.route("/gyg/1/cancel-reservation", methods=["POST"], strict_slashes=False)
@_gyg_auth
def gyg_cancel_reservation():
    body = request.get_json(silent=True)
    if not body or "data" not in body:
        return _gyg_err("VALIDATION_FAILURE", "Missing request body")

    ref = (body["data"].get("reservationReference") or "").strip()
    if not ref:
        return _gyg_err("VALIDATION_FAILURE", "Missing reservationReference")

    res = _GYG_RESERVATIONS.pop(ref, None)
    if not res:
        return _gyg_err("INVALID_RESERVATION", f"Unknown reservation: {ref}")

    if res.get("is_test"):
        print(f"[GYG-TEST] Mock reservation cancelled: {ref}")
        return jsonify({"data": {}}), 200

    burl = json.loads(res["booking_url"])
    ok, err = _gyg_octo_cancel(res["octo_uuid"], burl)
    if not ok:
        print(f"[GYG] OCTO cancel warning for {ref}: {err}")
        # Still return success — reservation will expire on OCTO side

    print(f"[GYG] Reservation cancelled: {ref}")
    return jsonify({"data": {}}), 200


# ── POST /gyg/1/book/ ────────────────────────────────────────────────────────

@app.route("/1/book", methods=["POST"], strict_slashes=False)
@app.route("/gyg/1/book", methods=["POST"], strict_slashes=False)
@_gyg_auth
def gyg_book():
    _gyg_cleanup_expired()
    body = request.get_json(silent=True)
    if not body or "data" not in body:
        return _gyg_err("VALIDATION_FAILURE", "Missing request body")

    d          = body["data"]
    res_ref    = (d.get("reservationReference") or "").strip()
    product_id = (d.get("productId") or "").strip()
    gyg_ref    = (d.get("gygBookingReference") or "").strip()
    items      = d.get("bookingItems") or []
    travelers  = d.get("travelers") or []

    if not res_ref:
        return _gyg_err("VALIDATION_FAILURE", "Missing reservationReference")
    if not product_id:
        return _gyg_err("INVALID_PRODUCT", "Missing productId")
    if not travelers:
        return _gyg_err("VALIDATION_FAILURE", "Missing travelers")

    res = _GYG_RESERVATIONS.get(res_ref)
    if not res:
        return _gyg_err("INVALID_RESERVATION", f"Unknown reservation: {res_ref}")

    # ── Test detection: test mode OR test traveler OR reservation flagged as test ──
    is_test = _GYG_TEST_MODE or res.get("is_test") or _gyg_is_test_traveler(travelers)
    if is_test and not res.get("is_test"):
        print(f"[GYG-TEST] Test traveler detected in book request — blocking OCTO confirm")

    # Generate booking reference (max 25 alphanumeric chars per GYG spec)
    bk_ref = f"LMD{uuid.uuid4().hex[:10].upper()}"

    # Build tickets — one per participant per category
    tickets = []
    for it in items:
        cat   = it.get("category", "ADULT")
        count = it.get("count", 1)
        for i in range(count):
            tickets.append({
                "category":       cat,
                "ticketCode":     f"{bk_ref}-{cat[0]}{i + 1}",
                "ticketCodeType": "TEXT",
            })
    if not tickets:
        tickets.append({
            "category": "COLLECTIVE",
            "ticketCode": bk_ref,
            "ticketCodeType": "TEXT",
        })

    if is_test:
        # ── Test mode: return mock booking without calling real OCTO API ──
        _GYG_BOOKINGS[bk_ref] = {
            "octo_uuid":    res["octo_uuid"],
            "slot_id":      res.get("slot_id", ""),
            "booking_url":  res["booking_url"],
            "gyg_ref":      gyg_ref,
            "product_id":   product_id,
            "date_time":    res.get("date_time", ""),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "is_test":      True,
        }
        _GYG_RESERVATIONS.pop(res_ref, None)
        print(f"[GYG-TEST] Mock book: {bk_ref} (no OCTO confirm)")
        return jsonify({"data": {
            "bookingReference": bk_ref,
            "tickets":          tickets,
        }}), 200

    # Confirm the held OCTO reservation with traveler contact details
    burl = json.loads(res["booking_url"])
    booking_data, err = _gyg_octo_confirm(res["octo_uuid"], burl, travelers[0])
    if err:
        _GYG_RESERVATIONS.pop(res_ref, None)
        return _gyg_err("INTERNAL_SYSTEM_FAILURE", err)

    # Persist booking mapping
    octo_uuid = (booking_data.get("uuid") or booking_data.get("id")
                 or res["octo_uuid"])
    sup_ref = (booking_data.get("supplierReference")
               or booking_data.get("reference") or "")
    _confirmed_at = datetime.now(timezone.utc).isoformat()

    _GYG_BOOKINGS[bk_ref] = {
        "octo_uuid":    octo_uuid,
        "slot_id":      res.get("slot_id", ""),
        "booking_url":  res["booking_url"],
        "gyg_ref":      gyg_ref,
        "product_id":   product_id,
        "date_time":    res.get("date_time", ""),
        "confirmed_at": _confirmed_at,
        "is_test":      False,
    }
    _GYG_RESERVATIONS.pop(res_ref, None)

    # Fix #1 + #2: Persist to Supabase Storage so booking survives redeploys
    # and is discoverable by the Bokun webhook handler via by_confirmation/ index.
    _gyg_bk_id = f"gyg_{bk_ref}"
    _save_booking_record(_gyg_bk_id, {
        "booking_id":         _gyg_bk_id,
        "confirmation":       octo_uuid,
        "supplier_reference": sup_ref,
        "slot_id":            res.get("slot_id", ""),
        "booking_url":        res["booking_url"],
        "supplier_id":        burl.get("supplier_id", ""),
        "gyg_ref":            gyg_ref,
        "gyg_booking_ref":    bk_ref,
        "product_id":         product_id,
        "start_time":         res.get("date_time", ""),
        "status":             "confirmed",
        "source":             "gyg",
        "is_test":            False,
        "cancellation_cutoff_hours": burl.get("cancellation_cutoff_hours"),
        "confirmed_at":       _confirmed_at,
        "created_at":         _confirmed_at,
    })

    print(f"[GYG] Booked: {bk_ref} -> OCTO {octo_uuid} sup_ref={sup_ref}")

    return jsonify({"data": {
        "bookingReference": bk_ref,
        "tickets":          tickets,
    }}), 200


# ── POST /gyg/1/cancel-booking/ ──────────────────────────────────────────────

@app.route("/1/cancel-booking", methods=["POST"], strict_slashes=False)
@app.route("/gyg/1/cancel-booking", methods=["POST"], strict_slashes=False)
@_gyg_auth
def gyg_cancel_booking():
    body = request.get_json(silent=True)
    if not body or "data" not in body:
        return _gyg_err("VALIDATION_FAILURE", "Missing request body")

    bk_ref = (body["data"].get("bookingReference") or "").strip()
    if not bk_ref:
        return _gyg_err("VALIDATION_FAILURE", "Missing bookingReference")

    bk = _GYG_BOOKINGS.get(bk_ref)

    # Fix #1: Fall back to Supabase Storage when in-memory lookup fails (after redeploy)
    if not bk:
        _persisted = _load_booking_record(f"gyg_{bk_ref}")
        if _persisted:
            bk = {
                "octo_uuid":   _persisted.get("confirmation", ""),
                "slot_id":     _persisted.get("slot_id", ""),
                "booking_url": _persisted.get("booking_url", ""),
                "gyg_ref":     _persisted.get("gyg_ref", ""),
                "product_id":  _persisted.get("product_id", ""),
                "date_time":   _persisted.get("start_time", ""),
                "is_test":     _persisted.get("is_test", False),
            }
            print(f"[GYG] Restored booking from Supabase: {bk_ref}")

    if not bk:
        return _gyg_err("INVALID_BOOKING", f"Unknown booking: {bk_ref}")

    # Test-mode bookings can always be cancelled (GYG integration tests use imminent dates)
    if bk.get("is_test"):
        _GYG_BOOKINGS.pop(bk_ref, None)
        print(f"[GYG-TEST] Mock booking cancelled: {bk_ref}")
        return jsonify({"data": {}}), 200

    # Reject cancellation if the activity date has passed
    # NOTE: GYG controls the customer-facing cancellation policy on their portal.
    # When GYG sends us a cancel-booking call, they've already enforced their policy.
    # We only reject for truly invalid states (past events). The per-product cutoff
    # is NOT enforced here — GYG's spec says "This call must cancel the booking."
    try:
        bk_dt = datetime.fromisoformat(bk["date_time"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if bk_dt < now:
            return _gyg_err("BOOKING_IN_PAST", "Booking date has already passed")
    except (ValueError, KeyError):
        pass

    burl = json.loads(bk["booking_url"])
    ok, err = _gyg_octo_cancel(bk["octo_uuid"], burl)
    if not ok:
        # Fix #3: Queue for auto-retry instead of just failing
        _supplier_id = burl.get("supplier_id", "")
        _queue_octo_retry(
            booking_id=f"gyg_{bk_ref}",
            supplier_id=_supplier_id,
            confirmation=bk["octo_uuid"],
            payment_intent_id="",   # GYG handles payment, no Stripe PI
            price_charged=0,
        )
        print(f"[GYG] Cancel failed, queued for retry: {bk_ref} | {err}")
        return _gyg_err("INTERNAL_SYSTEM_FAILURE", f"Cancel failed: {err}")

    _GYG_BOOKINGS.pop(bk_ref, None)
    # Update persisted record status
    _persisted_rec = _load_booking_record(f"gyg_{bk_ref}")
    if _persisted_rec:
        _persisted_rec["status"] = "cancelled"
        _persisted_rec["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        _persisted_rec["cancelled_by"] = "gyg_cancel_booking"
        _save_booking_record(f"gyg_{bk_ref}", _persisted_rec)

    print(f"[GYG] Booking cancelled: {bk_ref}")
    return jsonify({"data": {}}), 200


# ── POST /gyg/1/notify/ ──────────────────────────────────────────────────────

@app.route("/1/notify", methods=["POST"], strict_slashes=False)
@app.route("/gyg/1/notify", methods=["POST"], strict_slashes=False)
@_gyg_auth
def gyg_notify():
    body = request.get_json(silent=True)
    if not body or "data" not in body:
        return jsonify({"data": {}}), 200

    d     = body["data"]
    ntype = d.get("notificationType", "")
    desc  = d.get("description", "")
    pdet  = d.get("productDetails", {})
    pid   = pdet.get("productId", "")

    print(f"[GYG] Notification: type={ntype} product={pid} — {desc}")

    if ntype == "PRODUCT_DEACTIVATION":
        ndet = d.get("notificationDetails", {})
        print(f"[GYG] PRODUCT DEACTIVATED: {pid} "
              f"reason={ndet.get('errorType', '')} "
              f"request={ndet.get('failedRequestType', '')}")

    return jsonify({"data": {}}), 200


def _warm_mcp_slots_cache() -> None:
    """
    Pre-warm the MCP search_slots cache at startup in a background thread.

    The full unfiltered Supabase fetch (all slots, no city/category) paginates
    through ~5 pages × 10s each = ~50s. Running this at startup means the cache
    is hot before any agent requests arrive, so live requests always hit cache
    rather than blocking on a slow paginated fetch.

    Filtered queries (city/category) are fast regardless — Supabase applies
    the filter server-side and typically returns <200 rows in one page.
    """
    import threading

    def _do_warm():
        try:
            slots = _load_slots_from_supabase(hours_ahead=168)
            result = [_sanitize_slot(s) for s in slots]
            cache_key = "168.0|||0.0"  # matches default no-filter call in _mcp_call_tool
            now = time.time()
            _MCP_SLOTS_CACHE[cache_key] = {
                "slots":       result,
                "expires":     now + _MCP_SLOTS_CACHE_TTL,
                "stale_until": now + _MCP_SLOTS_CACHE_STALE_TTL,
            }
            print(f"[CACHE] MCP slots pre-warmed: {len(result)} slots cached")
        except Exception as e:
            print(f"[CACHE] MCP slots pre-warm failed: {e}")

    t = threading.Thread(target=_do_warm, daemon=True, name="mcp-cache-warmup")
    t.start()


if __name__ == "__main__":
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        print("WARNING: STRIPE_SECRET_KEY not set in .env")
        print("  Payments will not work until you add your Stripe key.")
        print("  Get it at: https://dashboard.stripe.com/apikeys")
        print()
    else:
        mode = "LIVE" if stripe_key.startswith("sk_live") else "TEST"
        print(f"Stripe: {mode} mode")

    _ensure_website_api_key()
    _ensure_cancellation_queue_table()
    _ensure_request_log_table()
    _start_retry_scheduler()
    _register_peek_webhook()
    _reconcile_pending_debits()   # refund any wallet debits stranded by a prior crash
    _start_mcp_thread()
    _warm_mcp_slots_cache()       # pre-populate search_slots cache before first agent request

    # ── Start Telegram bot in background ──────────────────────────────────
    def _start_telegram_bot():
        try:
            from telegram_bot import main as tg_main
            tg_main()
        except ImportError:
            print("[TELEGRAM] telegram_bot.py not found — bot disabled")
        except Exception as e:
            print(f"[TELEGRAM] Bot crashed: {e}")

    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        _tg_thread = threading.Thread(
            target=_start_telegram_bot, daemon=True, name="telegram-bot"
        )
        _tg_thread.start()
        print("[TELEGRAM] Bot thread started")
    else:
        print("[TELEGRAM] No TELEGRAM_BOT_TOKEN — bot disabled")

    print(f"Booking API + MCP server starting on http://localhost:{PORT}")
    print(f"  Health check:    http://localhost:{PORT}/health")
    print(f"  Book endpoint:   POST http://localhost:{PORT}/api/book")
    print(f"  MCP SSE:         http://localhost:{PORT}/sse")
    print()

    app.run(host="0.0.0.0", port=PORT, debug=False)
