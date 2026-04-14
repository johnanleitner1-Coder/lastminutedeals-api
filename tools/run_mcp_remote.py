"""
run_mcp_remote.py — Hosted remote MCP server for Last Minute Deals HQ.

Exposes last-minute tour/activity inventory as MCP tools via SSE transport.
Runs on Railway at https://mcp.lastminutedealshq.com

Unlike run_mcp_server.py (which reads local .tmp/ files), this server
calls the Railway booking API directly — no local setup required.

── Claude Desktop config ────────────────────────────────────────────────────
Add to %APPDATA%\\Claude\\claude_desktop_config.json:

{
  "mcpServers": {
    "lastminutedeals": {
      "url": "https://mcp.lastminutedealshq.com/sse"
    }
  }
}

── Claude Code config ───────────────────────────────────────────────────────
claude mcp add lastminutedeals --url https://mcp.lastminutedealshq.com/sse

── Environment variables ────────────────────────────────────────────────────
  BOOKING_API_URL        — Railway API base URL (e.g. https://web-production-dc74b.up.railway.app)
  LMD_WEBSITE_API_KEY    — Internal API key for the Railway booking API
  PORT                   — Server port (Railway sets this automatically)
"""

import os
import sys

import requests as _requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

BOOKING_API = os.getenv("BOOKING_API_URL", "https://web-production-dc74b.up.railway.app").rstrip("/")
API_KEY     = os.getenv("LMD_WEBSITE_API_KEY", "")
HDRS        = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

mcp = FastMCP(
    "Last Minute Deals HQ",
    instructions=(
        "You have access to real last-minute tour and activity inventory across "
        "Iceland, Italy, Morocco, Portugal, and more. Use search_slots to find "
        "available experiences, then book_slot to complete a reservation. "
        "Bookings are real — customers receive instant confirmation."
    ),
)


def _api_get(path: str, params: dict = None) -> dict | list:
    r = _requests.get(f"{BOOKING_API}{path}", headers=HDRS, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _api_post(path: str, body: dict) -> dict:
    r = _requests.post(f"{BOOKING_API}{path}", headers=HDRS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _safe_slot(s: dict) -> dict:
    """Strip internal fields before returning to agents."""
    return {
        "slot_id":           s.get("slot_id", ""),
        "category":          s.get("category", ""),
        "service_name":      s.get("service_name", ""),
        "business_name":     s.get("business_name", ""),
        "location_city":     s.get("location_city", ""),
        "location_country":  s.get("location_country", ""),
        "start_time":        s.get("start_time", ""),
        "end_time":          s.get("end_time", ""),
        "duration_minutes":  s.get("duration_minutes"),
        "hours_until_start": s.get("hours_until_start"),
        "spots_open":        s.get("spots_open"),
        "price":             s.get("price"),
        "our_price":         s.get("our_price"),
        "currency":          s.get("currency", "USD"),
        "confidence":        s.get("confidence", "high"),
    }


@mcp.tool()
def search_slots(
    city: str = "",
    category: str = "",
    hours_ahead: float = 72.0,
    max_price: float = 0.0,
    limit: int = 20,
) -> list[dict]:
    """
    Search for last-minute available tours and activities.

    Returns real inventory from Ventrata, Bokun, Zaui, and Peek Pro suppliers
    via the OCTO open booking protocol. Slots are sorted by urgency (soonest first).

    Args:
        city:        City or country filter, partial match (e.g. "Reykjavik", "Rome", "Iceland").
                     Leave empty to search all locations.
        category:    Category filter. Use "experiences" for tours/activities.
                     Leave empty for all categories.
        hours_ahead: Return slots starting within this many hours (default: 72).
        max_price:   Maximum price in USD. Set to 0 to return all prices.
        limit:       Max results to return (default: 20, max: 100).

    Returns:
        List of available slot dicts sorted by hours_until_start (soonest first).
    """
    params: dict = {"hours_ahead": hours_ahead, "limit": min(int(limit), 100)}
    if city:
        params["city"] = city
    if category:
        params["category"] = category
    if max_price and max_price > 0:
        params["max_price"] = max_price

    try:
        slots = _api_get("/slots", params)
        if not isinstance(slots, list):
            return [{"error": "Unexpected response from booking API"}]
        if not slots:
            return [{"message": f"No slots found for city={city!r} hours_ahead={hours_ahead}. Try expanding hours_ahead or clearing city filter."}]
        return [_safe_slot(s) for s in slots]
    except Exception as e:
        return [{"error": f"Could not fetch slots: {e}"}]


@mcp.tool()
def book_slot(
    slot_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
) -> dict:
    """
    Book a last-minute slot for a customer.

    Creates a Stripe Checkout Session and returns a checkout_url. Direct the
    customer to that URL to complete payment. The booking is confirmed with the
    supplier after payment succeeds. The customer receives an email confirmation.

    Args:
        slot_id:        Slot ID from search_slots results.
        customer_name:  Full name of the person attending.
        customer_email: Email address for booking confirmation.
        customer_phone: Phone number including country code (e.g. +15550001234).

    Returns:
        On success: { success: true, checkout_url, booking_id, expires_at }
        On error:   { success: false, error }
    """
    try:
        result = _api_post("/api/book", {
            "slot_id":        slot_id,
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
        })
        return result
    except _requests.HTTPError as e:
        try:
            detail = e.response.json()
            return {"success": False, "error": detail.get("error", str(e))}
        except Exception:
            return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_booking_status(booking_id: str) -> dict:
    """
    Check the status of a booking.

    Args:
        booking_id: The booking_id returned by book_slot.

    Returns:
        Booking record with status, confirmation number, and service details.
        Status values: pending, confirmed, failed, cancelled.
    """
    try:
        return _api_get(f"/bookings/{booking_id}")
    except _requests.HTTPError as e:
        if e.response.status_code == 404:
            return {"error": f"Booking '{booking_id}' not found."}
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_supplier_info() -> dict:
    """
    Returns information about the supplier network and available inventory.

    Use this to understand what destinations and experience types are available
    before calling search_slots.
    """
    return {
        "suppliers": [
            {
                "name": "Arctic Adventures",
                "destinations": ["Reykjavik", "Akureyri", "Iceland"],
                "categories": ["glacier hikes", "snowmobiling", "whale watching", "lava tunnels", "aurora", "diving"],
                "booking_platform": "Bokun",
            },
            {
                "name": "Bicycle Roma",
                "destinations": ["Rome"],
                "categories": ["e-bike tours", "cycling", "food tours", "day trips"],
                "booking_platform": "Bokun",
            },
            {
                "name": "Pure Morocco Experience",
                "destinations": ["Marrakech", "Merzouga", "Sahara"],
                "categories": ["desert tours", "cultural experiences"],
                "booking_platform": "Bokun",
            },
            {
                "name": "O Turista Tours",
                "destinations": ["Lisbon", "Porto", "Sintra", "Fatima", "Nazare"],
                "categories": ["city tours", "private tours", "day trips"],
                "booking_platform": "Bokun",
            },
            {
                "name": "Ventrata network",
                "destinations": ["Edinburgh", "global"],
                "categories": ["walking tours", "sightseeing", "experiences"],
                "booking_platform": "Ventrata",
            },
            {
                "name": "Zaui network",
                "destinations": ["Canada"],
                "categories": ["outdoor activities", "adventures"],
                "booking_platform": "Zaui",
            },
        ],
        "protocol": "OCTO (Open Connectivity for Tourism)",
        "confirmation": "instant",
        "payment": "Stripe — auth-then-capture (customer never charged for failed bookings)",
        "api_docs": "https://lastminutedealshq.com/developers",
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
