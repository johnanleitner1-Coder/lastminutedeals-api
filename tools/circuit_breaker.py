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

def _get_sb_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")

def _get_sb_secret() -> str:
    return os.getenv("SUPABASE_SECRET_KEY", "")

# Module-level aliases for backward compatibility (resolved at call time via _cb_headers)
SB_URL    = ""  # do not use directly — call _get_sb_url() to get current value
SB_SECRET = ""  # do not use directly — call _get_sb_secret() to get current value

FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS  = 300   # 5 minutes


def _cb_headers() -> dict:
    secret = _get_sb_secret()
    return {"apikey": secret, "Authorization": f"Bearer {secret}"}


def _cb_path(supplier_id: str) -> str:
    return f"circuit_breaker/{supplier_id}.json"


def _load_state(supplier_id: str) -> dict:
    """Load circuit breaker state for supplier. Returns default (closed) if not found."""
    if not _get_sb_url() or not _get_sb_secret():
        return {"state": "closed", "failures": 0}
    try:
        r = _req.get(
            f"{_get_sb_url()}/storage/v1/object/bookings/{_cb_path(supplier_id)}",
            headers=_cb_headers(), timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"state": "closed", "failures": 0, "last_failure_at": None, "tripped_at": None}


def _save_state(supplier_id: str, state: dict) -> None:
    if not _get_sb_url() or not _get_sb_secret():
        return
    try:
        _req.post(
            f"{_get_sb_url()}/storage/v1/object/bookings/{_cb_path(supplier_id)}",
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
                    # Cooldown elapsed — transition to half_open and allow exactly ONE probe.
                    # Mark probe_started_at so concurrent callers see half_open and are blocked.
                    state["state"]            = "half_open"
                    state["probe_started_at"] = now.isoformat()
                    _save_state(supplier_id, state)
                    return False, "half_open probe allowed"
            except Exception:
                pass

    if state.get("state") == "half_open":
        # Allow only one probe through. Concurrent calls are blocked until the probe resolves.
        probe_started = state.get("probe_started_at")
        if probe_started:
            try:
                probe_age = (now - datetime.fromisoformat(probe_started)).total_seconds()
                if probe_age < 30:
                    # Probe in flight — block concurrent callers
                    return True, f"Circuit half_open: probe in progress for {supplier_id}"
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
    """Return circuit breaker states for all suppliers — used in /metrics.
    Uses concurrent fetches to avoid N sequential round-trips (was O(n*5s) blocking).
    """
    sb_url = _get_sb_url()
    if not sb_url:
        return {}
    try:
        r = _req.post(
            f"{sb_url}/storage/v1/object/list/bookings",
            headers={**_cb_headers(), "Content-Type": "application/json"},
            json={"prefix": "circuit_breaker/", "limit": 100},
            timeout=5,
        )
        if r.status_code != 200:
            return {}
        items = r.json()
    except Exception:
        return {}

    import concurrent.futures as _cf

    def _fetch(item):
        name = item.get("name", "")
        # name may be "circuit_breaker/supplier.json" or just "supplier.json" depending on API version
        supplier_id = name.split("/")[-1].replace(".json", "")
        if not supplier_id:
            return None, None
        try:
            rec = _req.get(
                f"{sb_url}/storage/v1/object/bookings/{name}",
                headers=_cb_headers(), timeout=5,
            )
            if rec.status_code == 200:
                return supplier_id, rec.json()
        except Exception:
            pass
        return None, None

    states = {}
    with _cf.ThreadPoolExecutor(max_workers=min(len(items) or 1, 10)) as pool:
        for supplier_id, state in pool.map(_fetch, items):
            if supplier_id and state:
                states[supplier_id] = state
    return states
