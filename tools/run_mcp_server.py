"""
run_mcp_server.py — MCP server for LastMinuteDeals slot inventory.

Exposes last-minute slot inventory as MCP tools that Claude, GPT agents,
and any MCP-compatible AI framework can call directly.

Tools:
  search_slots         — find available slots by city / category / time window
  get_slot             — full details for a specific slot_id
  book_slot            — create a Stripe Checkout Session to complete a booking
  get_booking_status   — check status of a booking by booking_id
  refresh_slots        — trigger a fresh fetch (runs fetch_octo_slots.py)

── Claude Desktop config ────────────────────────────────────────────────────
Add to %APPDATA%\\Claude\\claude_desktop_config.json:

{
  "mcpServers": {
    "lastminutedeals": {
      "command": "C:/Users/janaa/AppData/Local/Programs/Python/Python313/python.exe",
      "args":    ["c:/Users/janaa/Agentic Workflows/tools/run_mcp_server.py"],
      "cwd":     "c:/Users/janaa/Agentic Workflows"
    }
  }
}

── Claude Code config ───────────────────────────────────────────────────────
Add to .claude/settings.json:

{
  "mcpServers": {
    "lastminutedeals": {
      "command": "python",
      "args":    ["tools/run_mcp_server.py"],
      "cwd":     "c:/Users/janaa/Agentic Workflows"
    }
  }
}

── HTTP mode (for testing / non-MCP clients) ────────────────────────────────
  python tools/run_mcp_server.py --http [--port 5051]

  GET  /health
  GET  /slots?city=NYC&category=wellness&hours_ahead=48&max_price=50&limit=20
  GET  /slots/<slot_id>
  POST /book   { slot_id, customer_name, customer_email, customer_phone }
  GET  /bookings/<booking_id>
  POST /refresh

── Required .env vars ───────────────────────────────────────────────────────
  STRIPE_SECRET_KEY   — enables real Stripe Checkout in book_slot
  LANDING_PAGE_URL    — used for Stripe success/cancel redirect URLs
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# ── Paths anchored to this file — safe regardless of launch cwd ──────────────

BASE_DIR       = Path(__file__).parent.parent            # c:/Users/janaa/Agentic Workflows
TMP_DIR        = BASE_DIR / ".tmp"
AGGREGATED     = TMP_DIR / "aggregated_slots.json"
OCTO_SLOTS     = TMP_DIR / "octo_slots.json"
BOOKINGS_LOG   = TMP_DIR / "mcp_bookings.json"

# Platforms fulfilled via pure HTTP API (no Playwright) — api_key_env stored in booking_url JSON
API_PLATFORMS = {
    "octo", "ventrata", "bokun", "xola", "peek", "zaui", "checkfront",  # OCTO standard
    "rezdy",   # Rezdy Agent API (own format, not OCTO — handled by RezdyBooker)
}

load_dotenv(BASE_DIR / ".env")
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(BASE_DIR / "tools"))
from normalize_slot import compute_hours_until  # noqa: E402


# ── Data layer ────────────────────────────────────────────────────────────────

def _recalc_hours(start_time_iso: str) -> float | None:
    """Recompute hours_until_start from start_time at query time."""
    try:
        s = start_time_iso
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((dt - datetime.now(timezone.utc)).total_seconds() / 3600, 2)
    except Exception:
        return None


def _load_slots() -> list[dict]:
    """
    Load slot data. Priority: aggregated_slots.json (all platforms merged).
    Falls back to individual platform files if aggregated not present.
    Always merges OCTO slots on top if available and not already in aggregated.
    Recalculates hours_until_start from start_time at load time.
    """
    all_slots: list[dict] = []
    seen_ids: set[str] = set()

    def _add(slots: list[dict]) -> None:
        for s in slots:
            sid = s.get("slot_id")
            if sid and sid not in seen_ids:
                s["hours_until_start"] = _recalc_hours(s.get("start_time", ""))
                all_slots.append(s)
                seen_ids.add(sid)

    # Try aggregated first (has everything including OCTO if aggregate_slots.py was run)
    if AGGREGATED.exists():
        try:
            slots = json.loads(AGGREGATED.read_text(encoding="utf-8"))
            if isinstance(slots, list) and slots:
                _add(slots)
        except Exception:
            pass

    # If aggregated is empty or missing, load individual platform files
    if not all_slots:
        for path in [OCTO_SLOTS, TMP_DIR / "rezdy_slots.json"]:
            if path.exists():
                try:
                    slots = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(slots, list):
                        _add(slots)
                except Exception:
                    continue

    # Always top up with API platform slots if not already captured via aggregated
    elif OCTO_SLOTS.exists():
        octo_in_agg = any(
            s.get("platform") in API_PLATFORMS for s in all_slots
        )
        if not octo_in_agg:
            try:
                slots = json.loads(OCTO_SLOTS.read_text(encoding="utf-8"))
                if isinstance(slots, list):
                    _add(slots)
            except Exception:
                pass

    all_slots.sort(key=lambda s: s.get("hours_until_start") or 9999)
    return all_slots


def _safe_slot(s: dict) -> dict:
    """Project fields safe to return to agents. Strips internal booking_url."""
    return {
        "slot_id":            s.get("slot_id", ""),
        "platform":           s.get("platform", ""),
        "category":           s.get("category", ""),
        "service_name":       s.get("service_name", ""),
        "business_name":      s.get("business_name", ""),
        "teacher":            s.get("teacher"),
        "location_city":      s.get("location_city", ""),
        "location_state":     s.get("location_state", ""),
        "start_time":         s.get("start_time", ""),
        "end_time":           s.get("end_time", ""),
        "duration_minutes":   s.get("duration_minutes"),
        "hours_until_start":  s.get("hours_until_start"),
        "spots_open":         s.get("spots_open"),
        "spots_total":        s.get("spots_total"),
        "price":              s.get("price"),
        "our_price":          s.get("our_price"),
        "currency":           s.get("currency", "USD"),
        "confidence":         s.get("confidence", "medium"),
    }


def _log_booking(record: dict) -> None:
    """Append a booking record to mcp_bookings.json."""
    try:
        TMP_DIR.mkdir(exist_ok=True)
        existing = []
        if BOOKINGS_LOG.exists():
            existing = json.loads(BOOKINGS_LOG.read_text(encoding="utf-8"))
        existing.append(record)
        BOOKINGS_LOG.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _update_booking(booking_id: str, updates: dict) -> None:
    """Find a booking by ID in mcp_bookings.json and merge in updates."""
    try:
        if not BOOKINGS_LOG.exists():
            return
        bookings = json.loads(BOOKINGS_LOG.read_text(encoding="utf-8"))
        for b in bookings:
            if b.get("booking_id") == booking_id:
                b.update(updates)
                break
        BOOKINGS_LOG.write_text(json.dumps(bookings, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _attempt_fulfillment(slot: dict, booking_id: str, customer: dict) -> dict:
    """
    Attempt booking fulfillment via complete_booking.py.
    Returns a result dict with keys: success, confirmation (or error).

    For API platforms (OCTO, Rezdy): called when api_key_env is set and the key
    is present in .env — fulfillment is pure HTTP.
    """
    try:
        # Lazy import — complete_booking has heavy deps (playwright)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "complete_booking", str(BASE_DIR / "tools" / "complete_booking.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        confirmation = mod.complete_booking(
            slot_id=slot.get("slot_id", ""),
            customer=customer,
            platform=slot.get("platform", ""),
            booking_url=slot.get("booking_url", ""),
        )
        _update_booking(booking_id, {
            "status":       "confirmed",
            "confirmation": confirmation,
            "fulfilled_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"success": True, "confirmation": confirmation}

    except Exception as exc:
        _update_booking(booking_id, {
            "status":            "fulfillment_failed",
            "fulfillment_error": str(exc),
        })
        return {"success": False, "error": str(exc)}


# ── Tool implementations ──────────────────────────────────────────────────────

def search_slots(
    city: str | None = None,
    category: str | None = None,
    hours_ahead: float = 48.0,
    max_price: float | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Search for last-minute available booking slots.

    Slots are sorted by urgency (soonest first). Only slots with open
    availability (spots_open > 0, or availability unknown) are returned.

    Args:
        city:        City name, case-insensitive partial match (e.g. "New York").
                     Omit for all cities.
        category:    Category filter: wellness, beauty, hospitality, experiences,
                     home_services, professional_services, events.
                     Omit to search all categories.
        hours_ahead: Return slots starting within this many hours (default: 48).
        max_price:   Maximum price in USD. Filters on our_price, then price.
        limit:       Max results (default: 20, max: 100).

    Returns:
        List of slot dicts sorted by hours_until_start ascending.
        Returns a help message if no slots match.
    """
    slots = _load_slots()

    if not slots:
        return [{"message": "No slot data loaded. Call refresh_slots to scrape fresh data."}]

    results = []
    for s in slots:
        h = s.get("hours_until_start")

        # Time window filter
        if h is None or h < 0 or h > hours_ahead:
            continue

        # Availability filter — exclude confirmed-full slots, include unknown
        spots_open = s.get("spots_open")
        if spots_open is not None and spots_open <= 0:
            continue

        # Category filter
        if category:
            if s.get("category", "").lower() != category.lower():
                continue

        # City filter
        if city:
            if city.lower() not in (s.get("location_city") or "").lower():
                continue

        # Exclude $0 / unpriced slots — not bookable
        price = float(s.get("our_price") or s.get("price") or 0)
        if price <= 0:
            continue

        # Price filter
        if max_price is not None and price > max_price:
            continue

        results.append(s)

    results.sort(key=lambda s: s.get("hours_until_start") or 9999)

    if not results:
        return [{
            "message": (
                f"No slots found for city={city!r} category={category!r} "
                f"hours_ahead={hours_ahead}. Try expanding hours_ahead, "
                "changing the category, or calling refresh_slots."
            )
        }]

    return [_safe_slot(s) for s in results[:min(limit, 100)]]


def get_slot(slot_id: str) -> dict:
    """
    Get full details for a specific slot.

    Args:
        slot_id: The slot_id from search_slots results.

    Returns:
        Slot dict, or an error dict if not found.
    """
    slots = _load_slots()
    for s in slots:
        if s.get("slot_id") == slot_id:
            return _safe_slot(s)
    return {
        "error": (
            f"Slot '{slot_id}' not found. It may have expired or the data "
            "needs refreshing — call refresh_slots and try again."
        )
    }


def book_slot(
    slot_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
) -> dict:
    """
    Book a last-minute slot for a customer.

    If Stripe is configured (STRIPE_SECRET_KEY in .env): creates a Stripe
    Checkout Session and returns a checkout_url. Direct the user to that URL
    to complete payment. Booking is confirmed after payment.

    If Stripe is not configured: logs the booking intent and returns a
    reference number. Use this mode for testing the full agent flow.

    Args:
        slot_id:        Slot ID from search_slots results.
        customer_name:  Full name of the person attending.
        customer_email: Email address for booking confirmation.
        customer_phone: Phone number (e.g. +15550001234).

    Returns:
        On success (Stripe):  { success, checkout_url, booking_id, expires_at }
        On success (no Stripe): { success, booking_id, note }
        On error:             { success: false, error }
    """
    # Find the slot (use raw slot for booking_url / platform)
    slots = _load_slots()
    slot = next((s for s in slots if s.get("slot_id") == slot_id), None)

    if not slot:
        return {"success": False, "error": "Slot not found or no longer available."}

    # Reject stale slots — recompute hours from current time rather than trusting cached value
    _fresh_hours = compute_hours_until(slot.get("start_time", ""))
    if _fresh_hours is not None and _fresh_hours < 0:
        return {"success": False, "error": "This slot has already started or passed."}

    spots_open = slot.get("spots_open")
    if spots_open is not None and spots_open <= 0:
        return {"success": False, "error": "No spots available for this slot."}

    stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()

    # ── No Stripe path ─────────────────────────────────────────────────────────
    if not stripe_key:
        booking_id = "LMD-" + str(uuid.uuid4())[:8].upper()
        record = {
            "booking_id":     booking_id,
            "slot_id":        slot_id,
            "service_name":   slot.get("service_name"),
            "business_name":  slot.get("business_name"),
            "start_time":     slot.get("start_time"),
            "booking_url":    slot.get("booking_url"),   # internal — not returned to agent
            "platform":       slot.get("platform"),
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "created_at":     datetime.now(timezone.utc).isoformat(),
            "status":         "pending_fulfillment",
        }
        _log_booking(record)

        # Attempt immediate fulfillment if credentials are configured for this platform
        platform = slot.get("platform", "")
        customer = {
            "name":  customer_name,
            "email": customer_email,
            "phone": customer_phone,
        }

        # API-fulfilled platforms (OCTO + Rezdy) — pure HTTP, no Playwright
        if platform in API_PLATFORMS:
            booking_params = {}
            try:
                booking_params = json.loads(slot.get("booking_url") or "{}")
            except Exception:
                pass
            api_key_env = booking_params.get("api_key_env", "")
            octo_key    = os.getenv(api_key_env, "").strip() if api_key_env else ""
            if octo_key:
                print(f"[book_slot] OCTO key found ({api_key_env}) — attempting fulfillment for {booking_id}")
                result = _attempt_fulfillment(slot, booking_id, customer)
                if result["success"]:
                    return {
                        "success":      True,
                        "booking_id":   booking_id,
                        "confirmation": result["confirmation"],
                        "service":      slot.get("service_name"),
                        "business":     slot.get("business_name"),
                        "start_time":   slot.get("start_time"),
                        "note":         f"Booking confirmed via OCTO ({platform}).",
                    }
                else:
                    return {
                        "success":    False,
                        "booking_id": booking_id,
                        "error": (
                            f"OCTO fulfillment failed: {result['error']}. "
                            "Booking intent logged — check .tmp/mcp_bookings.json."
                        ),
                    }

        return {
            "success":    True,
            "booking_id": booking_id,
            "service":    slot.get("service_name"),
            "business":   slot.get("business_name"),
            "start_time": slot.get("start_time"),
            "note": (
                "Booking logged. Set STRIPE_SECRET_KEY in .env for payment checkout."
            ),
        }

    # ── Stripe: create Checkout Session ───────────────────────────────────────
    our_price = slot.get("our_price") or slot.get("price")
    if not our_price or float(our_price) <= 0:
        # Price not set yet (compute_pricing.py hasn't run) — fall back to stub
        booking_id = "LMD-" + str(uuid.uuid4())[:8].upper()
        record = {
            "booking_id":     booking_id,
            "slot_id":        slot_id,
            "service_name":   slot.get("service_name"),
            "business_name":  slot.get("business_name"),
            "start_time":     slot.get("start_time"),
            "booking_url":    slot.get("booking_url"),
            "platform":       slot.get("platform"),
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "created_at":     datetime.now(timezone.utc).isoformat(),
            "status":         "pending_pricing",
        }
        _log_booking(record)
        return {
            "success":    True,
            "booking_id": booking_id,
            "service":    slot.get("service_name"),
            "business":   slot.get("business_name"),
            "start_time": slot.get("start_time"),
            "note": (
                "Booking logged. Pricing not yet configured for this slot — "
                "run compute_pricing.py to enable live checkout."
            ),
        }

    try:
        import stripe as stripe_lib
        stripe_lib.api_key = stripe_key

        landing = os.getenv("LANDING_PAGE_URL", "https://lastminutedeals.netlify.app").rstrip("/")
        service = (slot.get("service_name") or "Last-Minute Booking")[:80]
        city_st = f"{slot.get('location_city', '')} {slot.get('location_state', '')}".strip()
        starts  = (slot.get("start_time") or "")[:16].replace("T", " ")

        session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": slot.get("currency", "usd").lower(),
                    "product_data": {
                        "name": service,
                        "description": f"{city_st} — {starts}" if city_st else starts,
                    },
                    "unit_amount": int(float(our_price) * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=customer_email,
            success_url=f"{landing}?booking=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{landing}?booking=cancelled",
            metadata={
                "slot_id":        slot_id,
                "customer_name":  customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "booking_url":    slot.get("booking_url", ""),
                "platform":       slot.get("platform", ""),
                "source":         "mcp",
            },
        )

        record = {
            "booking_id":     session.id,
            "slot_id":        slot_id,
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "checkout_url":   session.url,
            "created_at":     datetime.now(timezone.utc).isoformat(),
            "status":         "pending_payment",
        }
        _log_booking(record)

        return {
            "success":      True,
            "checkout_url": session.url,
            "booking_id":   session.id,
            "expires_at":   datetime.fromtimestamp(
                session.expires_at, tz=timezone.utc
            ).isoformat(),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def get_booking_status(booking_id: str) -> dict:
    """
    Check the status of a booking.

    For Stripe bookings: queries Stripe directly.
    For non-Stripe bookings: checks the local bookings log.

    Args:
        booking_id: The booking_id returned from book_slot.

    Returns:
        { booking_id, status, payment_status, customer_email, amount_total }
    """
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()

    # ── Stripe lookup ──────────────────────────────────────────────────────────
    if stripe_key and booking_id.startswith("cs_"):
        try:
            import stripe as stripe_lib
            stripe_lib.api_key = stripe_key
            session = stripe_lib.checkout.Session.retrieve(booking_id)
            status_map = {"complete": "completed", "expired": "expired", "open": "pending"}
            return {
                "booking_id":     booking_id,
                "status":         status_map.get(session.get("status"), session.get("status")),
                "payment_status": session.get("payment_status", "unknown"),
                "customer_email": session.get("customer_email", ""),
                "amount_total":   session.get("amount_total"),
                "currency":       session.get("currency", "usd"),
            }
        except Exception as e:
            return {"booking_id": booking_id, "status": "error", "error": str(e)}

    # ── Local log lookup ───────────────────────────────────────────────────────
    if BOOKINGS_LOG.exists():
        try:
            bookings = json.loads(BOOKINGS_LOG.read_text(encoding="utf-8"))
            for b in bookings:
                if b.get("booking_id") == booking_id:
                    return {
                        "booking_id":     booking_id,
                        "status":         b.get("status", "unknown"),
                        "service_name":   b.get("service_name"),
                        "business_name":  b.get("business_name"),
                        "start_time":     b.get("start_time"),
                        "customer_name":  b.get("customer_name"),
                        "customer_email": b.get("customer_email"),
                        "created_at":     b.get("created_at"),
                    }
        except Exception:
            pass

    return {"booking_id": booking_id, "status": "not_found",
            "error": "Booking not found in local log or Stripe."}


def refresh_slots(hours_ahead: int = 72) -> dict:
    """
    Trigger a fresh fetch of all slot data (OCTO platforms).

    Runs fetch_octo_slots.py as a subprocess, then runs aggregate_slots.py
    to merge results. Call this when search_slots returns no results or
    data seems stale.

    Args:
        hours_ahead:  Fetch window in hours (default: 72).

    Returns:
        { success, message, slot_count, open_count, sources } or { success: false, error }
    """
    OCTO_SCRIPT      = BASE_DIR / "tools" / "fetch_octo_slots.py"
    AGGREGATE_SCRIPT = BASE_DIR / "tools" / "aggregate_slots.py"

    errors   = []
    sources  = []

    # ── OCTO fetch ────────────────────────────────────────────────────────────
    if OCTO_SCRIPT.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(OCTO_SCRIPT), "--hours-ahead", str(hours_ahead)],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                sources.append("octo")
            else:
                errors.append(f"OCTO: {(result.stderr or result.stdout or '')[-300:]}")
        except subprocess.TimeoutExpired:
            errors.append("OCTO fetch timed out after 120s")
        except Exception as e:
            errors.append(f"OCTO fetch error: {e}")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    if sources and AGGREGATE_SCRIPT.exists():
        try:
            subprocess.run(
                [sys.executable, str(AGGREGATE_SCRIPT), "--hours-ahead", str(hours_ahead)],
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            pass

    slots      = _load_slots()
    open_count = sum(1 for s in slots if (s.get("spots_open") or 1) > 0)

    if not sources and errors:
        return {"success": False, "error": "; ".join(errors)}

    return {
        "success":    True,
        "message":    (
            f"Refresh complete. {len(slots)} slots, {open_count} open. "
            f"Sources: {', '.join(sources) or 'none'}."
            + (f" Warnings: {'; '.join(errors)}" if errors else "")
        ),
        "slot_count": len(slots),
        "open_count": open_count,
        "sources":    sources,
    }


# ── MCP server (FastMCP, stdio transport) ─────────────────────────────────────

def run_mcp_stdio():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("mcp not installed. Run: python -m pip install mcp", file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP("lastminutedeals")

    # Register module-level functions directly as MCP tools.
    # FastMCP uses the function's name, type annotations, and docstring
    # to auto-generate the JSON Schema — no manual inputSchema needed.
    mcp.add_tool(search_slots)
    mcp.add_tool(get_slot)
    mcp.add_tool(book_slot)
    mcp.add_tool(get_booking_status)
    mcp.add_tool(refresh_slots)

    mcp.run()


# ── HTTP server (for testing / non-MCP API clients) ───────────────────────────

def run_http_server(port: int = 5051):
    try:
        from flask import Flask, jsonify, request as freq
    except ImportError:
        print("flask not installed. Run: python -m pip install flask", file=sys.stderr)
        sys.exit(1)

    app = Flask("lastminutedeals")

    @app.route("/health")
    def health():
        slots      = _load_slots()
        open_count = sum(1 for s in slots if (s.get("spots_open") or 1) > 0)
        by_platform: dict[str, int] = {}
        for s in slots:
            p = s.get("platform", "unknown")
            by_platform[p] = by_platform.get(p, 0) + 1
        return jsonify({
            "status":      "ok",
            "total_slots": len(slots),
            "open_slots":  open_count,
            "by_platform": by_platform,
            "data_files":  {
                "aggregated": AGGREGATED.exists(),
                "octo":       OCTO_SLOTS.exists(),
                "rezdy":      (TMP_DIR / "rezdy_slots.json").exists(),
            },
        })

    @app.route("/slots")
    def slots_endpoint():
        raw_cat = freq.args.get("category")          # None if not provided
        return jsonify(search_slots(
            city        = freq.args.get("city"),
            category    = raw_cat if raw_cat else None,   # omit = all categories
            hours_ahead = float(freq.args.get("hours_ahead", 48)),
            max_price   = float(freq.args["max_price"]) if "max_price" in freq.args else None,
            limit       = int(freq.args.get("limit", 20)),
        ))

    @app.route("/slots/<slot_id>")
    def slot_detail(slot_id):
        result = get_slot(slot_id)
        if "error" in result:
            return jsonify(result), 404
        return jsonify(result)

    @app.route("/book", methods=["POST"])
    def book_endpoint():
        data = freq.get_json(force=True, silent=True) or {}
        result = book_slot(
            slot_id        = data.get("slot_id", ""),
            customer_name  = data.get("customer_name", ""),
            customer_email = data.get("customer_email", ""),
            customer_phone = data.get("customer_phone", ""),
        )
        return jsonify(result), (200 if result.get("success") else 400)

    @app.route("/bookings/<booking_id>")
    def booking_status(booking_id):
        return jsonify(get_booking_status(booking_id))

    @app.route("/refresh", methods=["POST"])
    def refresh_endpoint():
        data = freq.get_json(force=True, silent=True) or {}
        return jsonify(refresh_slots(
            hours_ahead  = int(data.get("hours_ahead", 72)),
        ))

    print(f"\nLastMinuteDeals HTTP server on http://localhost:{port}")
    print(f"  GET  /health")
    print(f"  GET  /slots?city=NYC&category=wellness&hours_ahead=48")
    print(f"  GET  /slots/<slot_id>")
    print(f"  POST /book   {{slot_id, customer_name, customer_email, customer_phone}}")
    print(f"  GET  /bookings/<booking_id>")
    print(f"  POST /refresh\n")
    app.run(host="0.0.0.0", port=port, debug=False)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LastMinuteDeals MCP / HTTP server")
    parser.add_argument("--http",  action="store_true", help="Run as HTTP server instead of MCP stdio")
    parser.add_argument("--port",  type=int, default=5051, help="HTTP port (default: 5051)")
    args = parser.parse_args()

    if args.http:
        run_http_server(args.port)
    else:
        run_mcp_stdio()


if __name__ == "__main__":
    main()
