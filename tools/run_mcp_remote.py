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

import asyncio
import os
import time

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

BOOKING_API = os.getenv("BOOKING_API_URL", "https://web-production-dc74b.up.railway.app").rstrip("/")
API_KEY     = os.getenv("LMD_WEBSITE_API_KEY", "")
HDRS        = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
PORT        = int(os.getenv("PORT", "8080"))

# Shared async HTTP client — connection pool, keep-alive, no blocking.
# Separate connect vs read timeouts: fast-fail on connection, generous on read.
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_client  = httpx.AsyncClient(headers=HDRS, timeout=_TIMEOUT)

# Concurrency guard — cap simultaneous outbound API calls to prevent
# request storms from hammering the booking API or Supabase under burst load.
_SEMAPHORE = asyncio.Semaphore(10)

# ── Slot cache: serve identical searches from memory for 60 seconds ───────────
_SLOTS_CACHE: dict = {}      # params_key → {"slots": [...], "expires": float}
_SLOTS_CACHE_TTL = 60        # seconds

mcp = FastMCP(
    "Last Minute Deals HQ",
    host="0.0.0.0",
    port=PORT,
    instructions=(
        "You have access to real last-minute tour and activity inventory across "
        "Iceland, Italy, Morocco, Portugal, Japan, and more — sourced live from "
        "production booking systems via the OCTO open standard. "
        "Suppliers include Arctic Adventures (Iceland glacier hikes, snowmobiling, "
        "whale watching, aurora, lava tunnels), Bicycle Roma (Rome e-bike tours, "
        "food tours, day trips), Pure Morocco Experience (Sahara desert tours, "
        "Marrakech cultural experiences), Ramen Factory Kyoto (cooking classes, "
        "workshops), O Turista Tours (Lisbon, Porto, Sintra, Fatima day trips), "
        "Hillborn Experiences (Tanzania ultra-luxury safaris, Mount Kilimanjaro "
        "climbs, Zanzibar retreats, cultural encounters — East Africa), "
        "and more. "
        "Use search_slots to find available experiences, then book_slot to create "
        "a Stripe checkout session — the customer completes payment and receives "
        "instant confirmation. Bookings are real and go directly to the supplier."
    ),
)


async def _api_get(path: str, params: dict = None, retries: int = 2) -> dict | list:
    """Async GET with retry on transient failures. Never blocks the event loop."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            async with _SEMAPHORE:
                r = await _client.get(f"{BOOKING_API}{path}", params=params or {})
            r.raise_for_status()
            return r.json()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(0.5)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise   # 4xx — don't retry
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(0.5)
    raise last_exc


async def _api_post(path: str, body: dict) -> dict:
    """Async POST. Never blocks the event loop."""
    async with _SEMAPHORE:
        r = await _client.post(f"{BOOKING_API}{path}", json=body,
                               timeout=httpx.Timeout(30.0, connect=3.0))
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
async def search_slots(
    city: str = "",
    category: str = "",
    hours_ahead: float = 72.0,
    max_price: float = 0.0,
    limit: int = 20,
) -> list[dict]:
    """
    Search for last-minute available tours and activities.

    Returns real production inventory from Arctic Adventures, Bicycle Roma,
    Pure Morocco Experience, Ramen Factory Kyoto, O Turista Tours, Arctic Sea Tours,
    and more — sourced live via the OCTO open booking protocol.
    Slots are sorted by urgency (soonest first).

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

    # Serve from cache if the same query was made within the last 60 seconds
    cache_key = str(sorted(params.items()))
    now = time.time()
    cached = _SLOTS_CACHE.get(cache_key)
    if cached and cached["expires"] > now:
        return cached["slots"]

    try:
        raw = await _api_get("/slots", params)
        if not isinstance(raw, list):
            return [{"error": "Unexpected response from booking API"}]
        if not raw:
            return [{"message": (
                f"No slots found for city={city!r} hours_ahead={hours_ahead}. "
                "Try expanding hours_ahead or clearing city filter."
            )}]
        result = [_safe_slot(s) for s in raw]
        _SLOTS_CACHE[cache_key] = {"slots": result, "expires": now + _SLOTS_CACHE_TTL}
        return result
    except Exception as e:
        return [{"error": f"Could not fetch slots: {e}"}]


@mcp.tool()
async def book_slot(
    slot_id: str,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    quantity: int = 1,
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
        return await _api_post("/api/book", {
            "slot_id":        slot_id,
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "quantity":       max(1, int(quantity)),
        })
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()
            return {"success": False, "error": detail.get("error", str(e))}
        except Exception:
            return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def get_booking_status(booking_id: str) -> dict:
    """
    Check the status of a booking.

    Args:
        booking_id: The booking_id returned by book_slot.

    Returns:
        Booking record with status, confirmation number, and service details.
        Status values: pending, confirmed, failed, cancelled.
    """
    try:
        return await _api_get(f"/bookings/{booking_id}")
    except httpx.HTTPStatusError as e:
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
                "destinations": ["Reykjavik", "Husafell", "Skaftafell", "Iceland"],
                "categories": ["glacier hikes", "ice caves", "snowmobiling", "aurora tours",
                               "lava tunnels", "diving", "hiking", "whale watching",
                               "multi-day tours", "golden circle"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Arctic Sea Tours",
                "destinations": ["Dalvik", "North Iceland"],
                "categories": ["whale watching", "sea excursions"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Bicycle Roma",
                "destinations": ["Rome", "Appia Antica", "Castelli Romani", "Orvieto"],
                "categories": ["e-bike tours", "cycling", "food tours", "day trips",
                               "guided city tours", "bike rentals"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Pure Morocco Experience",
                "destinations": ["Marrakech", "Merzouga", "Sahara Desert"],
                "categories": ["desert tours", "multi-day tours", "cultural experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Ramen Factory Kyoto",
                "destinations": ["Kyoto", "Japan"],
                "categories": ["cooking classes", "ramen workshops", "cultural experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "O Turista Tours",
                "destinations": ["Lisbon", "Porto", "Sintra", "Fatima", "Nazare", "Sesimbra"],
                "categories": ["private tours", "day trips", "city tours",
                               "transfers", "wine experiences", "pilgrimage tours"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Hillborn Experiences",
                "destinations": ["Arusha", "Serengeti", "Zanzibar", "Kilimanjaro", "Tanzania"],
                "categories": ["private safaris", "Kilimanjaro climbs", "Zanzibar retreats",
                               "cultural encounters", "ultra-luxury tours", "wildlife"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
                "notes": "Ultra-luxury East African operator. $1M public liability insured.",
            },
        ],
        "live_slot_count": "1498 slots available within 168h (refreshed every 4h)",
        "protocol": "OCTO (Open Connectivity for Tourism) — direct supplier API, no scraping",
        "confirmation": "instant",
        "payment": "Stripe checkout — customer pays on our page, supplier confirmed automatically",
        "note": "All inventory is production. No test or demo slots.",
        "api_docs": "https://lastminutedealshq.com/developers",
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
