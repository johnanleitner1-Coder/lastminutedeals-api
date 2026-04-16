"""
intent_sessions.py — Persistent intent session engine for LastMinuteDeals.

An intent session is a named goal that the system pursues on behalf of an agent
until satisfied — regardless of how many API calls or how much time passes.

Instead of:
  Agent: "Execute this booking" → System: tries once, returns result

With intents:
  Agent: "Find and book a wellness slot in Detroit under $150"
  System: monitors continuously, tries automatically as new slots appear,
          notifies agent on completion or when action needed

Intent lifecycle:
  created → monitoring → executing → completed | failed | expired | cancelled

Persistence: .tmp/intent_sessions.json
Background monitoring: IntentMonitor thread polls every 60s for actionable intents

Intent schema:
  {
    "intent_id": "int_<24hex>",
    "api_key": "lmd_...",            # owning agent's API key
    "status": "monitoring",          # created|monitoring|executing|completed|failed|expired|cancelled
    "goal": "find_and_book",         # find_and_book | monitor_only | price_alert
    "constraints": {
      "category": "wellness",
      "city":     "Detroit",
      "budget":   150.0,
      "hours_ahead": 48,
      "allow_alternatives": true,
      "min_hours_ahead": 2,          # don't book if slot starts in < 2h (optional)
    },
    "customer": {                    # required for find_and_book
      "name":  "Jane Smith",
      "email": "jane@example.com",
      "phone": "+15550001234"
    },
    "payment": {
      "method": "wallet",            # "wallet" | "stripe_pi"
      "wallet_id": "wlt_...",
      "payment_intent_id": null
    },
    "autonomy": "full",              # "full" (auto-execute) | "notify" (alert before executing) | "monitor" (no execution)
    "callback_url": null,            # POST to this when status changes (optional)
    "ttl_hours": 24,                 # auto-expire after this many hours
    "created_at": "...",
    "expires_at": "...",
    "last_checked": "...",
    "attempt_count": 0,
    "result": null,                  # ExecutionResult dict when completed
    "notifications": []              # history of status change events sent
  }

Usage:
  from tools.intent_sessions import IntentSessionStore, IntentMonitor

  store   = IntentSessionStore()
  session = store.create(api_key, goal, constraints, customer, payment, autonomy, callback_url, ttl_hours)

  # In the API server background thread:
  monitor = IntentMonitor(store)
  monitor.start()
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests as _req

ROOT      = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent
SESSIONS_FILE = ROOT / ".tmp" / "intent_sessions.json"

sys.stdout.reconfigure(encoding="utf-8")

MONITOR_INTERVAL = 60  # seconds between monitor sweeps


# ── Persistence ───────────────────────────────────────────────────────────────

_sessions_lock = threading.Lock()

def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_sessions(sessions: dict):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


# ── Store ─────────────────────────────────────────────────────────────────────

class IntentSessionStore:
    """CRUD layer for intent sessions."""

    def create(
        self,
        api_key: str,
        goal: str,
        constraints: dict,
        customer: dict | None = None,
        payment: dict | None = None,
        autonomy: str = "full",
        callback_url: str | None = None,
        ttl_hours: int = 24,
    ) -> dict:
        """Create a new intent session. Returns the session record."""
        with _sessions_lock:
            sessions = _load_sessions()

            intent_id = "int_" + secrets.token_hex(12)
            now = datetime.now(timezone.utc)
            expires = now + timedelta(hours=ttl_hours)

            session = {
                "intent_id":    intent_id,
                "api_key":      api_key,
                "status":       "monitoring",
                "goal":         goal,
                "constraints":  constraints,
                "customer":     customer or {},
                "payment":      payment or {},
                "autonomy":     autonomy,
                "callback_url": callback_url,
                "ttl_hours":    ttl_hours,
                "created_at":   now.isoformat(),
                "expires_at":   expires.isoformat(),
                "last_checked": None,
                "attempt_count": 0,
                "result":        None,
                "notifications": [],
            }

            sessions[intent_id] = session
            _save_sessions(sessions)
            return session

    def get(self, intent_id: str) -> dict | None:
        with _sessions_lock:
            return _load_sessions().get(intent_id)

    def list_by_api_key(self, api_key: str) -> list[dict]:
        with _sessions_lock:
            return [s for s in _load_sessions().values() if s.get("api_key") == api_key]

    def update_status(self, intent_id: str, status: str, result: dict | None = None, note: str = ""):
        with _sessions_lock:
            sessions = _load_sessions()
            s = sessions.get(intent_id)
            if not s:
                return
            s["status"] = status
            if result:
                s["result"] = result
            if note:
                s.setdefault("notifications", []).append({
                    "ts":     datetime.now(timezone.utc).isoformat(),
                    "status": status,
                    "note":   note,
                })
            _save_sessions(sessions)

    def cancel(self, intent_id: str) -> bool:
        with _sessions_lock:
            sessions = _load_sessions()
            if intent_id not in sessions:
                return False
            sessions[intent_id]["status"] = "cancelled"
            _save_sessions(sessions)
            return True

    def actionable_sessions(self) -> list[dict]:
        """Return sessions that are currently monitoring and not expired."""
        now = datetime.now(timezone.utc)
        out = []
        with _sessions_lock:
            sessions = _load_sessions()
        for s in sessions.values():
            if s.get("status") not in ("monitoring",):
                continue
            expires = s.get("expires_at", "")
            if expires:
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if now > exp_dt:
                        continue
                except Exception:
                    pass
            out.append(s)
        return out

    def expire_old_sessions(self):
        """Mark expired sessions as 'expired'."""
        now = datetime.now(timezone.utc)
        with _sessions_lock:
            sessions = _load_sessions()
            changed = False
            for s in sessions.values():
                if s.get("status") not in ("monitoring",):
                    continue
                expires = s.get("expires_at", "")
                if not expires:
                    continue
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if now > exp_dt:
                        s["status"] = "expired"
                        s.setdefault("notifications", []).append({
                            "ts": now.isoformat(), "status": "expired", "note": "TTL elapsed without fulfillment"
                        })
                        changed = True
                except Exception:
                    pass
            if changed:
                _save_sessions(sessions)


# ── Callback dispatcher ───────────────────────────────────────────────────────

def _fire_callback(session: dict, event: str, payload: dict):
    """POST status change event to the session's callback_url (if set).
    Runs in a daemon thread to avoid blocking the monitor loop.
    """
    url = session.get("callback_url", "")
    if not url:
        return
    body = {
        "intent_id": session["intent_id"],
        "event":     event,
        "status":    session["status"],
        "ts":        datetime.now(timezone.utc).isoformat(),
        **payload,
    }

    def _post():
        try:
            _req.post(url, json=body, timeout=10)
        except Exception as e:
            print(f"[INTENT] Callback to {url} failed: {e}")

    import threading as _threading
    _threading.Thread(target=_post, daemon=True, name="intent-callback").start()


# ── Execution engine loader ───────────────────────────────────────────────────

def _load_execution_engine():
    spec = importlib.util.spec_from_file_location("execution_engine", TOOLS_DIR / "execution_engine.py")
    if not spec:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_booked_ids() -> set:
    booked_file = ROOT / ".tmp" / "booked_slots.json"
    if booked_file.exists():
        try:
            return set(json.loads(booked_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


# ── Intent executor ───────────────────────────────────────────────────────────

def execute_intent(session: dict, store: IntentSessionStore) -> bool:
    """
    Attempt to fulfill a single intent session.

    Returns True if the intent was resolved (success or permanent failure),
    False if it should remain in monitoring state for retry.
    """
    intent_id   = session["intent_id"]
    goal        = session.get("goal", "find_and_book")
    constraints = session.get("constraints", {})
    customer    = session.get("customer", {})
    payment     = session.get("payment", {})
    autonomy    = session.get("autonomy", "full")

    print(f"[INTENT] Checking {intent_id}: {goal} in {constraints.get('city','?')} ({constraints.get('category','any')})")

    # Update last_checked
    with _sessions_lock:
        sessions = _load_sessions()
        if intent_id in sessions:
            sessions[intent_id]["last_checked"] = datetime.now(timezone.utc).isoformat()
            sessions[intent_id]["attempt_count"] = sessions[intent_id].get("attempt_count", 0) + 1
            _save_sessions(sessions)

    # "monitor_only" and "price_alert" goals don't execute bookings
    if goal == "monitor_only":
        _check_slot_availability(session, store)
        return False  # keep monitoring

    if goal == "price_alert":
        _check_price_trigger(session, store)
        return False  # keep monitoring

    # "find_and_book"
    if goal != "find_and_book":
        print(f"[INTENT] Unknown goal: {goal}")
        return False

    if autonomy == "monitor":
        # Just check and notify — don't execute
        _check_slot_availability(session, store)
        return False

    # For "full" and "notify" autonomy — check if matching slots exist
    eng_mod = _load_execution_engine()
    if not eng_mod:
        print(f"[INTENT] execution_engine.py unavailable")
        return False

    req = eng_mod.ExecutionRequest(
        slot_id=constraints.get("slot_id"),
        category=constraints.get("category", ""),
        city=constraints.get("city", ""),
        hours_ahead=int(constraints.get("hours_ahead", 48)),
        budget=constraints.get("budget"),
        allow_alternatives=bool(constraints.get("allow_alternatives", True)),
        customer=customer,
        payment_method=payment.get("method", "wallet"),
        wallet_id=payment.get("wallet_id"),
        payment_intent_id=payment.get("payment_intent_id"),
    )

    # Check confidence before executing
    booked_ids = _load_booked_ids()
    engine = eng_mod.ExecutionEngine(booked_ids=booked_ids)
    confidence = engine._compute_confidence(req)

    if autonomy == "notify" and confidence >= 0.5:
        # Alert the agent — they decide whether to proceed
        store.update_status(intent_id, "monitoring",
            note=f"Matching slots found (confidence={confidence:.2f}). Awaiting your /intent/{intent_id}/execute call.")
        _fire_callback(session, "slots_available", {
            "confidence_score": confidence,
            "action_required":  f"POST /intent/{intent_id}/execute to proceed",
        })
        return False  # remain in monitoring, agent explicitly executes

    if confidence < 0.15:
        print(f"[INTENT] {intent_id}: no matching slots yet (confidence={confidence:.2f}), will retry")
        return False

    # "full" autonomy — execute now
    store.update_status(intent_id, "executing", note=f"Attempting booking (confidence={confidence:.2f})")
    _fire_callback(session, "executing", {"confidence_score": confidence})

    try:
        result = engine.execute(req)
    except Exception as _exec_err:
        # Execution raised unexpectedly — move out of "executing" so session isn't stuck forever.
        store.update_status(intent_id, "failed",
            note=f"Unhandled execution error: {_exec_err}")
        _fire_callback(session, "failed", {"error": str(_exec_err)[:300]})
        return False

    if result.success:
        store.update_status(intent_id, "completed", result=result.to_dict(),
            note=f"Booked: {result.service_name} ({result.confirmation})")
        _fire_callback(session, "completed", {
            "confirmation":      result.confirmation,
            "service_name":      result.service_name,
            "platform":          result.platform,
            "price_charged":     result.price_charged,
            "savings_vs_market": result.savings_vs_market,
            "attempts":          result.attempts,
        })

        # Send confirmation email
        try:
            spec = importlib.util.spec_from_file_location("send_booking_email",
                       TOOLS_DIR / "send_booking_email.py")
            mail_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mail_mod)
            mail_mod.send_booking_email(
                "booking_confirmed",
                customer.get("email", ""),
                customer.get("name", ""),
                {"service_name": result.service_name},
                confirmation_number=result.confirmation,
            )
        except Exception:
            pass

        return True  # intent resolved

    else:
        # Booking failed this cycle — keep monitoring unless permanent failure
        note = f"Attempt failed: {result.error}"
        store.update_status(intent_id, "monitoring", note=note)
        _fire_callback(session, "attempt_failed", {
            "error":   result.error,
            "attempts": result.attempts,
            "will_retry": True,
        })
        return False  # will retry on next monitor sweep


def _check_slot_availability(session: dict, store: IntentSessionStore):
    """For monitor_only / notify intents: fire callback if matching slots appear."""
    eng_mod = _load_execution_engine()
    if not eng_mod:
        return
    constraints = session.get("constraints", {})
    req = eng_mod.ExecutionRequest(
        category=constraints.get("category", ""),
        city=constraints.get("city", ""),
        hours_ahead=int(constraints.get("hours_ahead", 48)),
        budget=constraints.get("budget"),
        customer=session.get("customer") or {"name": "", "email": "", "phone": ""},
    )
    engine = eng_mod.ExecutionEngine(booked_ids=_load_booked_ids())
    confidence = engine._compute_confidence(req)
    if confidence >= 0.3:
        _fire_callback(session, "slots_available", {"confidence_score": confidence})


def _check_price_trigger(session: dict, store: IntentSessionStore):
    """For price_alert intents: fire callback when price drops below target."""
    target = session.get("constraints", {}).get("price_target")
    if not target:
        return
    agg = ROOT / ".tmp" / "aggregated_slots.json"
    if not agg.exists():
        return
    try:
        slots = json.loads(agg.read_text(encoding="utf-8"))
        constraints = session.get("constraints", {})
        city     = constraints.get("city", "").lower()
        category = constraints.get("category", "")
        for s in slots:
            if category and s.get("category") != category:
                continue
            if city and city not in (s.get("location_city") or "").lower():
                continue
            p = float(s.get("our_price") or s.get("price") or 0)
            if 0 < p <= float(target):
                _fire_callback(session, "price_alert", {
                    "slot_id":      s.get("slot_id"),
                    "service_name": s.get("service_name"),
                    "price":        p,
                    "price_target": target,
                })
                break
    except Exception as e:
        print(f"[INTENT] Price check error: {e}")


# ── Background monitor thread ─────────────────────────────────────────────────

class IntentMonitor:
    """
    Background thread that sweeps actionable intent sessions every MONITOR_INTERVAL
    seconds and attempts to fulfill them.

    Start once at API server boot:
        monitor = IntentMonitor(store)
        monitor.start()
    """

    def __init__(self, store: IntentSessionStore):
        self.store      = store
        self._stop      = threading.Event()
        self._thread    = threading.Thread(target=self._run, daemon=True, name="intent-monitor")

    def start(self):
        self._thread.start()
        print("[INTENT] Monitor thread started")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.store.expire_old_sessions()
                sessions = self.store.actionable_sessions()
                if sessions:
                    print(f"[INTENT] Sweeping {len(sessions)} active intent(s)")
                    for session in sessions:
                        try:
                            execute_intent(session, self.store)
                        except Exception as e:
                            print(f"[INTENT] Error processing {session.get('intent_id')}: {e}")
            except Exception as e:
                print(f"[INTENT] Monitor sweep error: {e}")

            self._stop.wait(MONITOR_INTERVAL)
