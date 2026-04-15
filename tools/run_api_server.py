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
from datetime import datetime, timezone, timedelta
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

# ── Idempotency store (in-memory, per-process) ────────────────────────────────
# Maps idempotency_key → booking_id for deduplication within a process lifetime.
# For cross-instance dedup, the Supabase Storage record acts as the persistent lock.
_IDEMPOTENCY_CACHE: dict = {}   # {key: {"booking_id": str, "result": dict}}

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
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if sb_url:
        try:
            requests.post(
                f"{sb_url}/storage/v1/object/bookings/{booking_id}.json",
                headers={**_sb_storage_headers(), "Content-Type": "application/json",
                         "x-upsert": "true"},
                data=json.dumps(record),
                timeout=8,
            )
        except Exception as e:
            print(f"[BOOKINGS] Supabase Storage write failed: {e}")
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

    import datetime as _dt
    scheduler = BackgroundScheduler()
    # Retry failed cancellations every 15 min (run immediately on startup)
    scheduler.add_job(_run_retry, "interval", minutes=15, id="retry_cancellations",
                      next_run_time=_dt.datetime.now())
    # Reconcile active bookings against platform every 30 min (first run after 5 min)
    scheduler.add_job(_run_reconcile, "interval", minutes=30, id="reconcile_bookings",
                      next_run_time=_dt.datetime.now() + _dt.timedelta(minutes=5))
    scheduler.start()
    print("Schedulers started — retry: every 15 min | reconcile: every 30 min")


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

def _load_slots_from_supabase(
    hours_ahead: float = 72,
    category: str = "",
    city: str = "",
    budget: float = 0,
    limit: int = 2000,
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
            hdrs   = {"apikey": sb_secret, "Authorization": f"Bearer {sb_secret}"}
            now_iso     = datetime.now(timezone.utc).isoformat()
            horizon_dt  = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
            horizon_iso = horizon_dt.isoformat()
            # Use list of tuples so requests sends two start_time params (PostgREST AND logic)
            param_list: list[tuple] = [
                ("limit", limit),
                ("order", "start_time.asc"),
                ("start_time", f"gt.{now_iso}"),    # exclude already-started slots
            ]
            if hours_ahead:
                param_list.append(("start_time", f"lte.{horizon_iso}"))
            if category:
                param_list.append(("category", f"eq.{category}"))
            if city:
                param_list.append(("location_city", f"ilike.%{city}%"))
            if budget:
                param_list.append(("our_price", f"lte.{budget}"))
            resp = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs,
                                params=param_list, timeout=10)
            if resp.status_code == 200:
                result = []
                for row in resp.json():
                    if row.get("raw"):
                        try:
                            result.append(json.loads(row["raw"]) if isinstance(row["raw"], str) else row["raw"])
                            continue
                        except Exception:
                            pass
                    result.append(row)
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


def _get_reliability_metrics() -> dict:
    """
    Compute real booking reliability stats from Supabase Storage booking records.
    Returns counts by status, success rate, reconciliation flags, and circuit breaker states.
    """
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not sb_url or not sb_secret:
        return {}

    stats = {
        "booked": 0, "cancelled": 0, "failed": 0,
        "reconciliation_required": 0, "total": 0,
        "success_rate": None, "circuit_breakers": {},
        "last_booking_at": None,
    }

    try:
        # List booking files (exclude cancellation_queue and circuit_breaker prefixes)
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
            and not item["name"].startswith("idem_")
        ]

        last_booking_at = None
        for name in names:
            try:
                rec = requests.get(
                    f"{sb_url}/storage/v1/object/bookings/{name}",
                    headers=_sb_storage_headers(), timeout=4,
                )
                if rec.status_code != 200:
                    continue
                record = rec.json()
                status = record.get("status", "unknown")
                if status in stats:
                    stats[status] += 1
                stats["total"] += 1
                # Track most recent booking
                ea = record.get("executed_at", "")
                if ea and (last_booking_at is None or ea > last_booking_at):
                    last_booking_at = ea
            except Exception:
                pass

        stats["last_booking_at"] = last_booking_at
        total = stats["total"]
        if total > 0:
            stats["success_rate"] = round(
                (stats["booked"] + stats["cancelled"]) / total, 3
            )
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

    return stats


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
        "reliability": _get_reliability_metrics(),
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
        result = {"success": True, "checkout_url": session.url}
        # Store idempotency record so duplicate requests return same URL
        if idempotency_key:
            _IDEMPOTENCY_CACHE[idempotency_key] = {"result": result}
            _save_booking_record(f"idem_{idempotency_key[:40]}", {
                "idempotency_key": idempotency_key,
                "checkout_url":    session.url,
                "slot_id":         slot_id,
                "created_at":      datetime.now(timezone.utc).isoformat(),
            })
        return jsonify(result)
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
                _booking_id_for_email = booking_record_id if "booking_record_id" in dir() else ""
                _cancel_url = (
                    f"{os.getenv('BOOKING_SERVER_HOST', '')}/cancel/{_booking_id_for_email}"
                    f"?t={_make_cancel_token(_booking_id_for_email)}"
                    if _booking_id_for_email else ""
                )
                send_booking_email("booking_confirmed", customer["email"], customer["name"],
                                   slot_for_email, confirmation_number=str(confirmation or ""),
                                   cancel_url=_cancel_url)
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
    Checks the circuit breaker before attempting OCTO platforms.
    """
    # ── Circuit breaker check for OCTO suppliers ──────────────────────────────
    octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
    supplier_id = platform
    try:
        burl_j = json.loads(booking_url) if isinstance(booking_url, str) and booking_url.startswith("{") else {}
        supplier_id = burl_j.get("supplier_id", platform)
    except Exception:
        pass
    is_octo = supplier_id in octo_platforms or platform == "octo"
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
            confirmation = module.complete_booking(
                slot_id=slot_id,
                customer=customer,
                platform=platform,
                booking_url=booking_url,
            )
            print(f"[FULFILLMENT] Confirmed: {confirmation}")
            # Record success with circuit breaker
            if is_octo:
                try:
                    cb_mod.record_success(supplier_id)
                except Exception:
                    pass
            return confirmation
    except FileNotFoundError:
        print(f"[FULFILLMENT] complete_booking.py not found — manual fulfillment needed")
    except Exception as e:
        # Record failure with circuit breaker
        if is_octo:
            try:
                cb_mod.record_failure(supplier_id, str(e)[:200])
            except Exception:
                pass
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
            now_iso     = datetime.now(timezone.utc).isoformat()
            param_list: list[tuple] = [
                ("limit", limit),
                ("order", "start_time.asc"),
                ("start_time", f"gt.{now_iso}"),   # exclude already-started slots
            ]
            if hours_ahead:
                horizon_iso = (datetime.now(timezone.utc) + timedelta(hours=hours_ahead)).isoformat()
                param_list.append(("start_time", f"lte.{horizon_iso}"))
            if category:
                param_list.append(("category", f"eq.{category}"))
            if city:
                param_list.append(("location_city", f"ilike.%{city}%"))   # % wildcards required for partial match
            if max_price is not None:
                param_list.append(("our_price", f"lte.{max_price}"))
            resp = requests.get(f"{sb_url}/rest/v1/slots", headers=hdrs, params=param_list, timeout=10)
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
    POST → MCP JSON-RPC (initialize, tools/list, tools/call)

    Backwards compatible: also accepts legacy { "tool": "...", "arguments": {} } format.
    """
    if request.method == "GET":
        return jsonify({
            "name": "Last Minute Deals HQ",
            "version": "1.0.0",
            "protocol": "MCP JSON-RPC 2.0",
            "endpoint": "POST /mcp",
            "tools": [t["name"] for t in _MCP_TOOLS],
            "docs": "https://lastminutedealshq.com/developers",
        })

    body   = request.get_json(force=True, silent=True) or {}
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

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
        try:
            result = _mcp_call_tool(tool_name, arguments)
            return jsonify({"tool": tool_name, "result": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Last Minute Deals HQ", "version": "1.0.0"},
        })
    elif method == "tools/list":
        return ok({"tools": _MCP_TOOLS})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments  = params.get("arguments", {})
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
        limit=2000,
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

    # Load slots from Supabase (fresh inventory, not Railway's local file)
    booked_ids = _load_booked()
    fresh_slots = _load_slots_from_supabase(
        hours_ahead=hours_ahead,
        category=category,
        city=city,
        budget=float(budget) if budget else 0,
        limit=2000,
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
                    "Authorization":     f"Bearer {api_key}",
                    "Octo-Capabilities": "octo/pricing",
                    "Content-Type":      "application/json",
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
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb_url:
        print(f"[CANCEL_QUEUE] Supabase not configured — cannot queue retry for {booking_id}")
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
    """Return booking status by ID. Used by MCP get_booking_status tool."""
    record = _load_booking_record(booking_id)
    if not record:
        return jsonify({"error": f"Booking '{booking_id}' not found."}), 404
    return jsonify({
        "booking_id":          record.get("booking_id", booking_id),
        "status":              record.get("status", "unknown"),
        "service_name":        record.get("service_name"),
        "business_name":       record.get("business_name"),
        "start_time":          record.get("start_time"),
        "customer_name":       record.get("customer_name"),
        "confirmation_number": record.get("confirmation_number") or record.get("confirmation"),
        "price_charged":       record.get("price_charged"),
        "currency":            record.get("currency", "USD"),
        "created_at":          record.get("created_at"),
    })


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


# ── Supplier-initiated cancellation (Bokun webhook) ───────────────────────────

def _find_booking_by_confirmation(confirmation_code: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Scan Supabase Storage bookings bucket for a record whose confirmation field
    matches confirmation_code. Returns (booking_id, record) or (None, None).
    """
    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb_url:
        return None, None
    headers = _sb_storage_headers()
    try:
        r = requests.post(
            f"{sb_url}/storage/v1/object/list/bookings",
            headers={**headers, "Content-Type": "application/json"},
            json={"prefix": "", "limit": 500, "offset": 0},
            timeout=10,
        )
        if r.status_code != 200:
            return None, None
        names = [
            item["name"] for item in r.json()
            if item.get("name", "").endswith(".json")
            and not item["name"].startswith("cancellation_queue/")
            and not item["name"].startswith("inbound_emails/")
        ]
    except Exception:
        return None, None

    for name in names:
        booking_id = name.replace(".json", "")
        record = _load_booking_record(booking_id)
        if record and record.get("confirmation") == confirmation_code:
            return booking_id, record
    return None, None


@app.route("/api/bokun/webhook", methods=["POST"])
def bokun_webhook():
    """
    Receive Bokun booking status change webhooks.

    When a supplier cancels a booking in their Bokun dashboard, Bokun POSTs
    here. We look up the booking by Bokun confirmation code, issue a Stripe
    refund, and email the customer.

    Register this URL in:
    Bokun Dashboard → Settings → Integrations → Webhooks → Add Endpoint
    URL: https://api.lastminutedealshq.com/api/bokun/webhook

    Bokun sends JSON with at minimum:
      { "type": "booking.cancelled", "booking": { "confirmationCode": "...", "status": "CANCELLED" } }
    """
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

    if request.method == "POST" and not already_done:
        # Execute cancellation inline (same logic as DELETE /bookings/{id})
        stripe_client  = _stripe()
        payment_intent = record.get("payment_intent_id", "")
        stripe_result  = {"success": True, "action": "no_payment_on_record"}

        if stripe_client.api_key and payment_intent:
            stripe_result = _refund_stripe(stripe_client, payment_intent)

        supplier_id  = record.get("supplier_id", record.get("platform", ""))
        confirmation = record.get("confirmation", "")
        octo_platforms = {"ventrata_edinexplore", "zaui_test", "peek_pro", "bokun_reseller"}
        if supplier_id in octo_platforms and confirmation:
            _cancel_octo_booking(supplier_id, confirmation)

        cancelled_at = datetime.now(timezone.utc).isoformat()
        record["status"]       = "cancelled"
        record["cancelled_at"] = cancelled_at
        record["cancelled_by"] = "customer_self_serve"
        _save_booking_record(booking_id, record)

        # Notify customer
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
                    refund_status="A full refund has been issued to your original payment method.",
                )
        except Exception:
            pass

        already_done = True

    if already_done:
        return f"""<!DOCTYPE html><html><head><title>Booking Cancelled</title></head>
        <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;padding:0 24px;">
        <div style="font-size:48px;margin-bottom:16px;">✓</div>
        <h2 style="color:#0f172a;">Booking cancelled</h2>
        <p style="color:#64748b;">Your booking for <strong>{service}</strong> has been cancelled
        and a full refund has been issued. It typically appears within 3–5 business days.</p>
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
    </body></html>"""


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
    """
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
    if api_key != valid_key:
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
            "Search for last-minute available tours and activities. Returns real inventory "
            "from Bokun (Arctic Adventures, Bicycle Roma, Pure Morocco Experience, O Turista, "
            "Factory Alliance Kyoto, Boka Bliss Montenegro, TourTransfer Bucharest), "
            "Ventrata, Zaui, and Peek Pro via the OCTO open booking protocol. "
            "Slots are sorted by urgency (soonest first)."
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
                "city":        {"type": "string",  "description": "City or country filter, partial match (e.g. 'Rome', 'Iceland'). Leave empty for all locations."},
                "category":    {"type": "string",  "description": "Category filter (e.g. 'experiences'). Leave empty for all."},
                "hours_ahead": {"type": "number",  "description": "Return slots starting within this many hours. Default: 72."},
                "max_price":   {"type": "number",  "description": "Maximum price in USD. Omit or set to 0 for all prices."},
                "limit":       {"type": "integer", "description": "Max number of results to return. Default: 20, max: 100."},
            },
        },
    },
    {
        "name": "book_slot",
        "description": (
            "Book a last-minute slot for a customer. Creates a Stripe Checkout Session and "
            "returns a checkout_url. Direct the customer to that URL to complete payment. "
            "The booking is confirmed with the supplier after payment succeeds. "
            "The customer receives an email confirmation. Bookings are real."
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
                "slot_id":        {"type": "string", "description": "Slot ID from search_slots results. Required."},
                "customer_name":  {"type": "string", "description": "Full name of the person attending the experience."},
                "customer_email": {"type": "string", "description": "Email address where booking confirmation will be sent."},
                "customer_phone": {"type": "string", "description": "Phone number including country code (e.g. +15550001234)."},
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
]

def _mcp_call_tool(name: str, arguments: dict) -> dict:
    """Execute an MCP tool call and return result."""
    api_key = os.getenv("LMD_WEBSITE_API_KEY", "")
    hdrs = {"X-API-Key": api_key, "Content-Type": "application/json"}
    base = f"http://localhost:{PORT}"

    if name == "search_slots":
        params = {}
        if arguments.get("city"):        params["city"]        = arguments["city"]
        if arguments.get("category"):    params["category"]    = arguments["category"]
        if arguments.get("hours_ahead"): params["hours_ahead"] = arguments["hours_ahead"]
        if arguments.get("max_price"):   params["max_price"]   = arguments["max_price"]
        params["limit"] = arguments.get("limit", 20)
        r = requests.get(f"{base}/slots", headers=hdrs, params=params, timeout=15)
        return r.json()

    elif name == "book_slot":
        r = requests.post(f"{base}/api/book", headers=hdrs, json=arguments, timeout=30)
        return r.json()

    elif name == "get_booking_status":
        bid = arguments.get("booking_id", "")
        r = requests.get(f"{base}/bookings/{bid}", headers=hdrs, timeout=10)
        return r.json()

    elif name == "get_supplier_info":
        return {
            "suppliers": [
                {"name": "Arctic Adventures", "destinations": ["Reykjavik", "Iceland"], "platform": "Bokun"},
                {"name": "Bicycle Roma", "destinations": ["Rome"], "platform": "Bokun"},
                {"name": "Pure Morocco Experience", "destinations": ["Marrakech", "Merzouga", "Sahara"], "platform": "Bokun"},
                {"name": "O Turista Tours", "destinations": ["Lisbon", "Porto", "Sintra", "Fatima", "Nazare"], "platform": "Bokun"},
                {"name": "Factory Alliance Kyoto", "destinations": ["Kyoto", "Japan"], "platform": "Bokun"},
                {"name": "Boka Bliss", "destinations": ["Kotor", "Montenegro"], "platform": "Bokun"},
                {"name": "TourTransfer Bucharest", "destinations": ["Bucharest", "Romania"], "platform": "Bokun"},
                {"name": "Ventrata network", "destinations": ["Edinburgh", "global"], "platform": "Ventrata"},
                {"name": "Zaui network", "destinations": ["Canada"], "platform": "Zaui"},
            ],
            "protocol": "OCTO",
            "confirmation": "instant",
            "docs": "https://lastminutedealshq.com/developers",
        }

    else:
        return {"error": f"Unknown tool: {name}"}


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
            "You have access to real last-minute tour and activity inventory across "
            "Iceland, Italy, Morocco, Portugal, Japan, Tanzania, Finland, Montenegro, "
            "Romania, and more — sourced live from production booking systems via the "
            "OCTO open standard. "
            "Suppliers include Arctic Adventures (Iceland glacier hikes, snowmobiling, "
            "whale watching, aurora, lava tunnels), Bicycle Roma (Rome e-bike tours, "
            "food tours, day trips), Pure Morocco Experience (Sahara desert tours, "
            "Marrakech cultural experiences), Ramen Factory Kyoto (cooking classes, "
            "workshops), O Turista Tours (Lisbon, Porto, Sintra, Fatima day trips), "
            "Arctic Sea Tours (North Iceland whale watching), "
            "Hillborn Experiences (Tanzania ultra-luxury safaris, Mount Kilimanjaro "
            "climbs, Zanzibar retreats — East Africa). "
            "Use search_slots to find available experiences, then book_slot to create "
            "a Stripe checkout session. Bookings are real and go directly to the supplier."
        ),
    )

    def _safe(s):
        return {k: s.get(k) for k in (
            "slot_id", "category", "service_name", "business_name",
            "location_city", "location_country", "start_time", "end_time",
            "duration_minutes", "hours_until_start", "spots_open",
            "price", "our_price", "currency", "confidence",
        )}

    @mcp.tool()
    def search_slots(city: str = "", category: str = "", hours_ahead: float = 72.0,
                     max_price: float = 0.0, limit: int = 20) -> list[dict]:
        """Search last-minute tours and activities. Returns live production inventory
        sorted by urgency (soonest first).
        Args: city (partial match, e.g. "Reykjavik"), category (e.g. "experiences"),
        hours_ahead (default 72), max_price (0=no limit), limit (max 100)."""
        p = {"hours_ahead": hours_ahead, "limit": min(int(limit), 100)}
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
    def book_slot(slot_id: str, customer_name: str, customer_email: str,
                  customer_phone: str) -> dict:
        """Book a slot. Returns checkout_url for the customer to complete Stripe payment.
        Args: slot_id (from search_slots), customer_name, customer_email,
        customer_phone (with country code, e.g. +15550001234)."""
        try:
            r = _mcp_req.post(f"{BOOKING_API}/api/book", headers=_HDRS, json={
                "slot_id": slot_id, "customer_name": customer_name,
                "customer_email": customer_email, "customer_phone": customer_phone,
            }, timeout=15)
            r.raise_for_status()
            return r.json()
        except _mcp_req.HTTPError as e:
            try:
                return {"success": False, "error": e.response.json().get("error", str(e))}
            except Exception:
                return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_booking_status(booking_id: str) -> dict:
        """Check booking status by booking_id. Returns status, confirmation number, service details."""
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
        """Returns supplier network info. Call before search_slots to understand available destinations."""
        return {
            "suppliers": [
                {"name": "Arctic Adventures", "destinations": ["Reykjavik", "Husafell", "Skaftafell", "Iceland"],
                 "categories": ["glacier hikes", "ice caves", "snowmobiling", "aurora", "diving", "whale watching"]},
                {"name": "Arctic Sea Tours", "destinations": ["Dalvik", "North Iceland"],
                 "categories": ["whale watching", "sea excursions"]},
                {"name": "Bicycle Roma", "destinations": ["Rome", "Appia Antica", "Castelli Romani"],
                 "categories": ["e-bike tours", "food tours", "day trips", "city tours"]},
                {"name": "Pure Morocco Experience", "destinations": ["Marrakech", "Merzouga", "Sahara"],
                 "categories": ["desert tours", "multi-day tours", "cultural experiences"]},
                {"name": "Ramen Factory Kyoto", "destinations": ["Kyoto", "Japan"],
                 "categories": ["cooking classes", "ramen workshops"]},
                {"name": "O Turista Tours", "destinations": ["Lisbon", "Porto", "Sintra", "Fatima"],
                 "categories": ["private tours", "day trips", "transfers"]},
                {"name": "Hillborn Experiences", "destinations": ["Arusha", "Serengeti", "Zanzibar", "Kilimanjaro"],
                 "categories": ["private safaris", "Kilimanjaro climbs", "Zanzibar retreats", "ultra-luxury tours"],
                 "notes": "Ultra-luxury East African operator. $1M public liability insured."},
            ],
            "protocol": "OCTO via Bokun — direct supplier API, production inventory only",
            "payment": "Stripe checkout, instant supplier confirmation",
        }

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
    resp = requests.post(mcp_url, json=request.get_json(silent=True),
                         params=request.args, timeout=30)
    return jsonify(resp.json()), resp.status_code


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
    _register_peek_webhook()
    _start_mcp_thread()

    print(f"Booking API + MCP server starting on http://localhost:{PORT}")
    print(f"  Health check:    http://localhost:{PORT}/health")
    print(f"  Book endpoint:   POST http://localhost:{PORT}/api/book")
    print(f"  MCP SSE:         http://localhost:{PORT}/sse")
    print()

    app.run(host="0.0.0.0", port=PORT, debug=False)
