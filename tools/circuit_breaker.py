"""
Per-supplier circuit breaker.

Tracks consecutive failures per OCTO supplier in Supabase Storage
(bookings/circuit_breaker/{supplier_id}.json).

States:
  closed   — normal operation, requests flow through
  open     — supplier tripped, requests blocked for COOLDOWN_SECONDS
  half_open — cooldown elapsed, one probe allowed through to test recovery

Thresholds:
  FAILURE_THRESHOLD  = 5 consecutive failures → trip to open
  COOLDOWN_SECONDS   = 300 (5 minutes)

Used by run_api_server.py before any OCTO booking or cancellation attempt.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests as _req
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SB_URL    = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_SECRET = os.getenv("SUPABASE_SECRET_KEY", "")

FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS  = 300   # 5 minutes


def _cb_headers() -> dict:
    return {"apikey": SB_SECRET, "Authorization": f"Bearer {SB_SECRET}"}


def _cb_path(supplier_id: str) -> str:
    return f"circuit_breaker/{supplier_id}.json"


def _load_state(supplier_id: str) -> dict:
    """Load circuit breaker state for supplier. Returns default (closed) if not found."""
    if not SB_URL or not SB_SECRET:
        return {"state": "closed", "failures": 0}
    try:
        r = _req.get(
            f"{SB_URL}/storage/v1/object/bookings/{_cb_path(supplier_id)}",
            headers=_cb_headers(), timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"state": "closed", "failures": 0, "last_failure_at": None, "tripped_at": None}


def _save_state(supplier_id: str, state: dict) -> None:
    if not SB_URL or not SB_SECRET:
        return
    try:
        _req.post(
            f"{SB_URL}/storage/v1/object/bookings/{_cb_path(supplier_id)}",
            headers={**_cb_headers(), "Content-Type": "application/json", "x-upsert": "true"},
            data=json.dumps(state),
            timeout=5,
        )
    except Exception:
        pass


def is_open(supplier_id: str) -> tuple[bool, str]:
    """
    Returns (blocked, reason).
    blocked=True means do NOT attempt the booking — circuit is open.
    """
    state = _load_state(supplier_id)
    now   = datetime.now(timezone.utc)

    if state.get("state") == "open":
        tripped_at = state.get("tripped_at")
        if tripped_at:
            try:
                elapsed = (now - datetime.fromisoformat(tripped_at)).total_seconds()
                if elapsed < COOLDOWN_SECONDS:
                    remaining = int(COOLDOWN_SECONDS - elapsed)
                    return True, f"Circuit open for {supplier_id} — {remaining}s cooldown remaining ({state.get('failures', 0)} consecutive failures)"
                else:
                    # Cooldown elapsed — move to half-open (allow one probe)
                    state["state"] = "half_open"
                    _save_state(supplier_id, state)
                    return False, "half_open probe allowed"
            except Exception:
                pass

    return False, "closed"


def record_success(supplier_id: str) -> None:
    """
    Call after a successful OCTO request. Resets failure count and closes circuit.
    """
    state = _load_state(supplier_id)
    if state.get("failures", 0) > 0 or state.get("state") != "closed":
        state["state"]    = "closed"
        state["failures"] = 0
        state["last_success_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(supplier_id, state)


def record_failure(supplier_id: str, reason: str = "") -> None:
    """
    Call after a failed OCTO request. Increments failure count.
    Trips the circuit after FAILURE_THRESHOLD consecutive failures.
    """
    state    = _load_state(supplier_id)
    failures = state.get("failures", 0) + 1
    now      = datetime.now(timezone.utc).isoformat()

    state["failures"]        = failures
    state["last_failure_at"] = now
    state["last_error"]      = reason[:200]

    if failures >= FAILURE_THRESHOLD:
        if state.get("state") != "open":
            state["state"]      = "open"
            state["tripped_at"] = now
            print(f"[CIRCUIT_BREAKER] ⚠ {supplier_id} TRIPPED after {failures} failures: {reason[:100]}")
    else:
        state["state"] = "closed"

    _save_state(supplier_id, state)


def get_all_states() -> dict:
    """Return circuit breaker states for all suppliers — used in /metrics."""
    try:
        r = _req.post(
            f"{SB_URL}/storage/v1/object/list/bookings",
            headers={**_cb_headers(), "Content-Type": "application/json"},
            json={"prefix": "circuit_breaker/", "limit": 100},
            timeout=5,
        )
        if r.status_code != 200:
            return {}
        states = {}
        for item in r.json():
            name = item.get("name", "")
            supplier_id = name.replace("circuit_breaker/", "").replace(".json", "")
            if supplier_id:
                rec = _req.get(
                    f"{SB_URL}/storage/v1/object/bookings/{name}",
                    headers=_cb_headers(), timeout=5,
                )
                if rec.status_code == 200:
                    states[supplier_id] = rec.json()
        return states
    except Exception:
        return {}
