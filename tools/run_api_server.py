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
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
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

def _signing_secret() -> str:
    """Return the HMAC signing secret, generating and persisting one if absent."""
    key = os.getenv("LMD_SIGNING_SECRET", "")
    if key:
        return key
    secret_file = Path(".tmp/.signing_secret")
    if secret_file.exists():
        return secret_file.read_text().strip()
    key = secrets.token_hex(32)
    secret_file.write_text(key)
    return key

def _sign_record(record: dict) -> str:
    """Return HMAC-SHA256 hex signature over the stable JSON representation."""
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hmac.new(_signing_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()

def _load_bookings() -> dict:
    """Load all booking records. Supabase is primary; local file is fallback."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            r = requests.get(
                f"{sb_url}/rest/v1/bookings",
                headers={"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"},
                params={"select": "*", "limit": "1000"},
                timeout=5,
            )
            if r.status_code == 200:
                return {row["booking_id"]: row for row in r.json()}
        except Exception:
            pass
    # Local fallback
    if _BOOKINGS_FILE.exists():
        try:
            return json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_booking_record(booking_id: str) -> dict | None:
    """Load a single booking record by ID. Supabase primary, local file fallback."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            r = requests.get(
                f"{sb_url}/rest/v1/bookings",
                headers={"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"},
                params={"booking_id": f"eq.{booking_id}", "select": "*", "limit": "1"},
                timeout=5,
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    return rows[0]
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
    """Persist a booking record. Writes to Supabase (primary) and local file (backup)."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            requests.post(
                f"{sb_url}/rest/v1/bookings",
                headers={
                    "apikey":        sb_secret,
                    "Authorization": f"Bearer {sb_secret}",
                    "Content-Type":  "application/json",
                    "Prefer":        "resolution=merge-duplicates",
                },
                json=record,
                timeout=8,
            )
        except Exception as e:
            print(f"[BOOKINGS] Supabase write failed: {e} — falling back to local file")
    # Always write local copy as backup
    try:
        local_bookings = {}
        if _BOOKINGS_FILE.exists():
            try:
                local_bookings = json.loads(_BOOKINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        local_bookings[booking_id] = record
        _BOOKINGS_FILE.write_text(json.dumps(local_bookings, indent=2), encoding="utf-8")
    except Exception:
        pass

def _make_receipt(result_dict: dict, customer_email: str = "") -> dict:
    """
    Build a signed execution receipt from a result dict.
    Stored in bookings.json and returned in every successful execution response.
    """
    booking_id = result_dict.get("booking_id") or f"bk_{secrets.token_hex(8)}"
    record = {
        "booking_id":     booking_id,
        "confirmation":   result_dict.get("confirmation", ""),
        "platform":       result_dict.get("platform", ""),
        "service_name":   result_dict.get("service_name", ""),
        "price_charged":  result_dict.get("price_charged"),
        "status":         result_dict.get("status", ""),
        "executed_at":    datetime.now(timezone.utc).isoformat(),
        "customer_email": customer_email,
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

    # Live bookable slot count
    agg = Path(".tmp/aggregated_slots.json")
    if agg.exists():
        try:
            slots = json.loads(agg.read_text(encoding="utf-8"))
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
    Create the Supabase `cancellation_queue` table if it doesn't exist.
    Runs at server startup — safe to call multiple times (IF NOT EXISTS).
    Requires psycopg2-binary and SUPABASE_DB_URL in .env.
    Fails silently if the DB is unreachable (table likely already exists).
    """
    db_url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not db_url:
        return
    try:
        import psycopg2  # type: ignore
        stripped  = db_url.replace("postgresql://", "").replace("postgres://", "")
        userinfo, hostinfo = stripped.rsplit("@", 1)
        user, password     = userinfo.split(":", 1)
        host_port, dbname  = hostinfo.rsplit("/", 1)
        host, port         = host_port.rsplit(":", 1)
        conn = psycopg2.connect(
            host=host, port=int(port), dbname=dbname,
            user=user, password=password, sslmode="require",
            connect_timeout=8,
        )
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id        TEXT PRIMARY KEY,
                confirmation      TEXT,
                platform          TEXT,
                supplier_id       TEXT,
                booking_url       TEXT,
                service_name      TEXT,
                price_charged     FLOAT,
                status            TEXT DEFAULT 'booked',
                executed_at       TIMESTAMPTZ,
                cancelled_at      TIMESTAMPTZ,
                customer_email    TEXT,
                payment_intent_id TEXT,
                slot_id           TEXT,
                cancellation_details JSONB,
                signature         TEXT
            );
            CREATE TABLE IF NOT EXISTS cancellation_queue (
                booking_id        TEXT PRIMARY KEY,
                confirmation      TEXT NOT NULL,
                supplier_id       TEXT NOT NULL,
                payment_intent_id TEXT,
                price_charged     FLOAT,
                octo_cancelled    BOOLEAN DEFAULT FALSE,
                attempts          INTEGER DEFAULT 0,
                last_attempt_at   TIMESTAMPTZ,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                status            TEXT DEFAULT 'pending_octo',
                error_detail      TEXT
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("cancellation_queue table ready")
    except Exception as e:
        # Non-fatal — table may already exist or DB is temporarily unreachable
        print(f"cancellation_queue table setup skipped: {e}")


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

    # In gunicorn multi-worker mode, only start scheduler in the master/first worker
    # to avoid duplicate runs. Check via an env sentinel.
    if os.environ.get("_RETRY_SCHEDULER_STARTED"):
        return
    os.environ["_RETRY_SCHEDULER_STARTED"] = "1"

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

    scheduler = BackgroundScheduler()
    # Run immediately on startup, then every 15 minutes
    scheduler.add_job(_run_retry, "interval", minutes=15, id="retry_cancellations",
                      next_run_time=__import__("datetime").datetime.now())
    scheduler.start()
    print("Cancellation retry scheduler started (every 15 min)")


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
PORT        = int(os.getenv("BOOKING_SERVER_PORT", "5050"))


def _load_booked() -> set:
    """Load set of slot_ids that have already been booked."""
    if BOOKED_FILE.exists():
        try:
            return set(json.loads(BOOKED_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def _mark_booked(slot_id: str) -> None:
    booked = _load_booked()
    booked.add(slot_id)
    BOOKED_FILE.write_text(json.dumps(list(booked)), encoding="utf-8")

# ── API key system ────────────────────────────────────────────────────────────
API_KEYS_FILE = Path(".tmp/api_keys.json")

def _load_api_keys() -> dict:
    if API_KEYS_FILE.exists():
        try:
            return json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_api_keys(keys: dict) -> None:
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_KEYS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")

def _validate_api_key(key: str) -> bool:
    if not key:
        return False
    keys = _load_api_keys()
    if key not in keys:
        return False
    # Increment usage count
    keys[key]["usage_count"] = keys[key].get("usage_count", 0) + 1
    keys[key]["last_used"] = datetime.now(timezone.utc).isoformat()
    _save_api_keys(keys)
    return True

def _generate_api_key() -> str:
    import secrets
    return "lmd_" + secrets.token_hex(24)

# ── Stripe customers (saved payment methods) ──────────────────────────────────
CUSTOMERS_FILE = Path(".tmp/stripe_customers.json")

def _load_customers() -> dict:
    if CUSTOMERS_FILE.exists():
        try:
            return json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_customers(customers: dict) -> None:
    CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOMERS_FILE.write_text(json.dumps(customers, indent=2), encoding="utf-8")

# ── Webhook subscriptions ─────────────────────────────────────────────────────
WEBHOOKS_FILE = Path(".tmp/webhook_subscriptions.json")

def _load_webhooks() -> dict:
    if WEBHOOKS_FILE.exists():
        try:
            return json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_webhooks(subs: dict) -> None:
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBHOOKS_FILE.write_text(json.dumps(subs, indent=2), encoding="utf-8")

# ── Lazy Stripe import ────────────────────────────────────────────────────────
def _stripe():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    return stripe


# ── Slot lookup ───────────────────────────────────────────────────────────────
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
    return jsonify({"status": "ok", "slots": slot_count})


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

    if DATA_FILE.exists():
        try:
            slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))
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
        except Exception:
            pass

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

    if not all([slot_id, customer_name, customer_email, customer_phone]):
        return jsonify({"success": False, "error": "Missing required fields."}), 400

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

    price_cents  = int(float(our_price) * 100)
    service_name = slot.get("service_name", "Last-Minute Booking")[:80]
    landing_url  = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").rstrip("/")

    try:
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
                "quantity": 1,
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
            },
        )
        return jsonify({"success": True, "checkout_url": session.url})
    except Exception as e:
        print(f"Stripe error: {e}")
        return jsonify({"success": False, "error": "Payment system error. Please try again."}), 500


@app.route("/api/webhook", methods=["POST"])
def stripe_webhook():
    """
    Handle Stripe webhook events.
    On checkout.session.completed: fulfill the booking on the source platform.
    """
    stripe           = _stripe()
    webhook_secret   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    payload          = request.get_data()
    sig_header       = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session          = event["data"]["object"]
        metadata         = session.get("metadata", {})

        # ── Wallet top-up: credit the wallet immediately ───────────────────────
        if metadata.get("event_type") == "wallet_topup":
            wid    = metadata.get("wallet_id", "")
            amount = int(metadata.get("amount_cents", 0))
            if wid and amount > 0:
                try:
                    import importlib.util as _ilu
                    spec = _ilu.spec_from_file_location("manage_wallets",
                                Path(__file__).parent / "manage_wallets.py")
                    wlt_mod = _ilu.module_from_spec(spec)
                    spec.loader.exec_module(wlt_mod)
                    wlt_mod.credit_wallet(wid, amount, "Stripe top-up")
                    print(f"[WEBHOOK] Wallet {wid} credited ${amount/100:.2f}")
                except Exception as wlt_err:
                    print(f"[WEBHOOK] Wallet credit failed: {wlt_err}")
            return jsonify({"status": "ok"})

        slot_id          = metadata.get("slot_id", "")
        payment_intent   = session.get("payment_intent", "")
        customer = {
            "name":  metadata.get("customer_name", ""),
            "email": metadata.get("customer_email", session.get("customer_email", "")),
            "phone": metadata.get("customer_phone", ""),
        }
        platform    = metadata.get("platform", "")
        booking_url = metadata.get("booking_url", "")

        print(f"[WEBHOOK] Card authorized: slot={slot_id} customer={customer['email']} pi={payment_intent}")

        # Look up slot for email context
        slot_for_email = get_slot_by_id(slot_id) or {"service_name": metadata.get("service_name", "your booking")}

        # ── Send "booking in progress" email immediately ───────────────────────
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_initiated", customer["email"], customer["name"], slot_for_email)
        except Exception as mail_err:
            print(f"[WEBHOOK] Initiated email failed (non-fatal): {mail_err}")

        # ── Attempt booking BEFORE capturing payment ──────────────────────────
        try:
            confirmation = _fulfill_booking(slot_id, customer, platform, booking_url)

            # Booking succeeded — capture the held payment
            if payment_intent:
                stripe.PaymentIntent.capture(payment_intent)
                print(f"[WEBHOOK] Payment captured: {payment_intent}")

            _mark_booked(slot_id)
            print(f"[WEBHOOK] Booking confirmed: {confirmation}")

            # Persist booking record for cancellation / verification
            booking_record_id = f"bk_{slot_id[:12]}"
            _burl_raw = metadata.get("booking_url", "")
            try:
                _burl_j = json.loads(_burl_raw) if isinstance(_burl_raw, str) and _burl_raw.startswith("{") else {}
                _supplier_id = _burl_j.get("supplier_id", platform)
            except Exception:
                _supplier_id = platform
            _save_booking_record(booking_record_id, {
                "booking_id":        booking_record_id,
                "confirmation":      str(confirmation or ""),
                "platform":          platform,
                "supplier_id":       _supplier_id,
                "booking_url":       _burl_raw,
                "service_name":      metadata.get("service_name", ""),
                "price_charged":     session.get("amount_total", 0) / 100,
                "status":            "booked",
                "executed_at":       datetime.now(timezone.utc).isoformat(),
                "customer_email":    customer["email"],
                "payment_intent_id": payment_intent,
                "slot_id":           slot_id,
            })

            # ── Send confirmation email ────────────────────────────────────────
            try:
                from send_booking_email import send_booking_email
                send_booking_email("booking_confirmed", customer["email"], customer["name"],
                                   slot_for_email, confirmation_number=str(confirmation or ""))
            except Exception as mail_err:
                print(f"[WEBHOOK] Confirmation email failed (non-fatal): {mail_err}")

        except Exception as e:
            print(f"[WEBHOOK] Booking failed: {e}")

            # Booking failed — cancel the hold so the customer is never charged
            if payment_intent:
                try:
                    stripe.PaymentIntent.cancel(payment_intent)
                    print(f"[WEBHOOK] Payment hold cancelled (customer not charged): {payment_intent}")
                except Exception as cancel_err:
                    print(f"[WEBHOOK] Failed to cancel hold: {cancel_err} — manual action required")

            # ── Send failure email ─────────────────────────────────────────────
            try:
                from send_booking_email import send_booking_email
                send_booking_email("booking_failed", customer["email"], customer["name"],
                                   slot_for_email, error_reason=str(e))
            except Exception as mail_err:
                print(f"[WEBHOOK] Failure email failed (non-fatal): {mail_err}")

    return jsonify({"status": "ok"})


def _fulfill_booking(slot_id: str, customer: dict, platform: str, booking_url: str):
    """
    Execute the booking on the source platform after payment is confirmed.
    Imports complete_booking.py (Playwright automation).
    """
    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "complete_booking",
            Path(__file__).parent / "complete_booking.py"
        )
        if spec:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            confirmation = module.complete_booking(
                slot_id=slot_id,
                customer=customer,
                platform=platform,
                booking_url=booking_url,
            )
            print(f"[FULFILLMENT] Confirmed: {confirmation}")
            return confirmation
    except FileNotFoundError:
        print(f"[FULFILLMENT] complete_booking.py not found — manual fulfillment needed")
    except Exception as e:
        raise e


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
    hours_ahead = request.args.get("hours_ahead", 72, type=int)
    max_price   = request.args.get("max_price", type=float)
    limit       = min(request.args.get("limit", 50, type=int), 500)

    if sb_url and sb_secret:
        try:
            hdrs = {"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"}
            params: dict = {"limit": limit, "order": "hours_until_start.asc"}
            if category:
                params["category"] = f"eq.{category}"
            if city:
                params["location_city"] = f"ilike.{city}"
            if hours_ahead:
                params["hours_until_start"] = f"lte.{hours_ahead}"
            if max_price is not None:
                params["our_price"] = f"lte.{max_price}"
            resp = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs, params=params, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                # Restore full slot from raw JSONB where available
                result = []
                for row in rows:
                    if row.get("raw"):
                        try:
                            result.append(json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"])
                            continue
                        except Exception:
                            pass
                    result.append(row)
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
        result.append(s)
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
    except stripe.error.CardError as e:
        return jsonify({"success": False, "error": f"Card declined: {e.user_message}"}), 402
    except Exception as e:
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
        confirmation = _fulfill_booking(slot_id, customer, slot.get("platform", ""), slot.get("booking_url", ""))
        stripe.PaymentIntent.capture(pi.id)
        _mark_booked(slot_id)

        # Persist booking record so DELETE /bookings/{id} can cancel it later
        booking_record_id = f"bk_{slot_id[:12]}"
        booking_url_raw = slot.get("booking_url", "")
        try:
            _burl = json.loads(booking_url_raw) if isinstance(booking_url_raw, str) else (booking_url_raw or {})
            _supplier_id = _burl.get("supplier_id", slot.get("platform", ""))
        except Exception:
            _burl = {}
            _supplier_id = slot.get("platform", "")
        _save_booking_record(booking_record_id, {
            "booking_id":        booking_record_id,
            "confirmation":      str(confirmation or ""),
            "platform":          slot.get("platform", ""),
            "supplier_id":       _supplier_id,
            "booking_url":       booking_url_raw,
            "service_name":      slot.get("service_name", ""),
            "price_charged":     float(our_price),
            "status":            "booked",
            "executed_at":       datetime.now(timezone.utc).isoformat(),
            "customer_email":    customer_email,
            "payment_intent_id": pi.id,
            "slot_id":           slot_id,
        })

        # Send confirmation email
        try:
            from send_booking_email import send_booking_email
            send_booking_email("booking_confirmed", customer_email, customer_name, slot,
                               confirmation_number=str(confirmation or ""))
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


@app.route("/mcp", methods=["POST"])
def mcp_http():
    """
    MCP-over-HTTP endpoint. Allows any agent to call our MCP tools without
    requiring the MCP stdio transport.

    Body: { "tool": "search_last_minute_slots", "arguments": { "city": "NYC", "category": "wellness" } }
    Returns: { "result": [...] }

    Available tools: search_last_minute_slots, get_slot_details, book_slot, get_booking_status
    """
    data      = request.get_json(force=True, silent=True) or {}
    tool_name = (data.get("tool") or "").strip()
    arguments = data.get("arguments") or {}

    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent))
        import run_mcp_server as mcp_mod

        if tool_name == "search_last_minute_slots":
            result = mcp_mod.search_last_minute_slots(**{
                k: v for k, v in arguments.items()
                if k in ("category", "city", "hours_ahead", "max_price", "limit")
            })
        elif tool_name == "get_slot_details":
            result = mcp_mod.get_slot_details(arguments.get("slot_id", ""))
        elif tool_name == "book_slot":
            result = mcp_mod.book_slot(
                slot_id=arguments.get("slot_id", ""),
                customer_name=arguments.get("customer_name", ""),
                customer_email=arguments.get("customer_email", ""),
                customer_phone=arguments.get("customer_phone", ""),
            )
        elif tool_name == "get_booking_status":
            result = mcp_mod.get_booking_status(arguments.get("booking_id", ""))
        else:
            available = ["search_last_minute_slots", "get_slot_details", "book_slot", "get_booking_status"]
            return jsonify({"error": f"Unknown tool: {tool_name}", "available_tools": available}), 400

        return jsonify({"tool": tool_name, "result": result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    agg = Path(".tmp/aggregated_slots.json")
    if not agg.exists():
        return jsonify({"success": False, "error": "No slot data available."}), 503

    slots = json.loads(agg.read_text(encoding="utf-8"))
    booked_ids = _load_booked()

    # Filter candidates
    now = datetime.now(timezone.utc)
    candidates = []
    for s in slots:
        if s.get("slot_id") in booked_ids:
            continue
        h = s.get("hours_until_start") or 999
        if h < 0 or h > hours_ahead:
            continue
        p = float(s.get("our_price") or s.get("price") or 0)
        if p <= 0:
            continue
        if budget and p > float(budget):
            continue
        if category and s.get("category") != category:
            continue
        if city and city.lower() not in (s.get("location_city") or "").lower():
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

    engine = eng_mod.ExecutionEngine(booked_ids=booked_ids)
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
        "confirmation": "EVT-12345",
        "slot_id": "...",
        "service_name": "60-min Deep Tissue Massage",
        "platform": "mindbody",
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

    # Load booked IDs
    booked_ids = _load_booked()

    engine = eng_mod.ExecutionEngine(booked_ids=booked_ids)
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
        receipt = _make_receipt(result_dict, c_email)
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
        "confirmation": "LUMA-XYZ",
        "platform": "luma",
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

    return jsonify({**record, "verified": signature_valid}), 200


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

    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(2 ** attempt)   # 2s then 4s
        try:
            r = requests.delete(
                f"{base_url}/bookings/{confirmation}",
                headers={
                    "Authorization":     f"Bearer {api_key}",
                    "Octo-Capabilities": "octo/pricing",
                    "Content-Type":      "application/json",
                },
                timeout=15,
            )
            if r.status_code in (200, 204):
                return {"success": True, "detail": f"Cancelled on {platform} (HTTP {r.status_code})"}
            if r.status_code == 404:
                # Booking not found = already cancelled or expired — desired state
                return {"success": True, "detail": "Booking not found on supplier (already cancelled or expired)"}
            if r.status_code in (400, 401, 403, 422):
                # Permanent client error — retrying won't help
                return {"success": False, "permanent": True,
                        "detail": f"Permanent failure HTTP {r.status_code}: {r.text[:200]}"}
            # 5xx or unexpected — log and retry
            last_error = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last_error = str(e)

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
    Persist a failed OCTO cancellation to Supabase `cancellation_queue`.
    The retry_cancellations.py cron picks this up every 15 min and retries automatically.
    """
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        print(f"[CANCEL_QUEUE] Supabase not configured — cannot queue retry for {booking_id}")
        return
    try:
        r = requests.post(
            f"{sb_url}/rest/v1/cancellation_queue",
            headers={
                "apikey":        sb_secret,
                "Authorization": f"Bearer {sb_secret}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates",
            },
            json={
                "booking_id":        booking_id,
                "confirmation":      confirmation,
                "supplier_id":       supplier_id,
                "payment_intent_id": payment_intent_id,
                "price_charged":     price_charged,
                "octo_cancelled":    False,
                "attempts":          0,
                "created_at":        datetime.now(timezone.utc).isoformat(),
                "status":            "pending_octo",
                "error_detail":      None,
            },
            timeout=10,
        )
        if r.ok:
            print(f"[CANCEL_QUEUE] Queued for automatic retry: {booking_id} | {supplier_id} | {confirmation}")
        else:
            print(f"[CANCEL_QUEUE] Supabase upsert failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[CANCEL_QUEUE] Could not queue {booking_id}: {e}")


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
    record = _load_booking_record(booking_id)
    if not record:
        return jsonify({"success": False, "error": "Booking not found"}), 404

    if record.get("status") == "cancelled":
        return jsonify({"success": True, "booking_id": booking_id, "status": "cancelled",
                        "message": "Booking was already cancelled"}), 200

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
    cancelled_at = datetime.now(timezone.utc).isoformat()
    record["status"]       = "cancelled"
    record["cancelled_at"] = cancelled_at
    record["cancellation_details"] = {"stripe": stripe_result, "octo": octo_result}
    _save_booking_record(booking_id, record)

    return jsonify({
        "success":        True,
        "booking_id":     booking_id,
        "status":         "cancelled",
        "stripe_result":  stripe_result.get("action"),
        "refund_id":      stripe_result.get("refund_id"),
        "platform_result": octo_result.get("detail"),
        "octo_queued_for_retry": octo_queued,
        "cancelled_at":   cancelled_at,
    }), 200


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
    if wlt.get("api_key") != api_key and not _validate_api_key(api_key):
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
    if wlt.get("api_key") != api_key and not _validate_api_key(api_key):
        return jsonify({"error": "Unauthorized."}), 401

    txs = wlt.get("transactions", [])[-50:]
    return jsonify({"wallet_id": wallet_id, "transactions": txs})


# ── Main ──────────────────────────────────────────────────────────────────────

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
    _start_retry_scheduler()

    print(f"Booking API starting on http://localhost:{PORT}")
    print(f"  Health check: http://localhost:{PORT}/health")
    print(f"  Book endpoint: POST http://localhost:{PORT}/api/book")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=False)
