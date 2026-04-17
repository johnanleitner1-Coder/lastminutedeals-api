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

BOOKING_API = (os.getenv("BOOKING_API_URL") or "https://web-production-dc74b.up.railway.app").rstrip("/")
API_KEY     = os.getenv("LMD_WEBSITE_API_KEY", "")
HDRS        = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
PORT        = int(os.getenv("PORT", "8080"))

# Shared async HTTP client — connection pool, keep-alive, no blocking.
# connect=8s: gives Railway time to wake from sleep (cold starts take 10-30s but
# 8s covers most cases; the retry loop provides two more chances after that).
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_client  = httpx.AsyncClient(headers=HDRS, timeout=_TIMEOUT)

# Concurrency guard — cap simultaneous outbound API calls to prevent
# request storms from hammering the booking API or Supabase under burst load.
_SEMAPHORE = asyncio.Semaphore(10)

# ── Slot cache ────────────────────────────────────────────────────────────────
# Fresh TTL: 300s (5 min). Inventory is refreshed every 4h — 5 min is safe.
# Stale TTL: 1800s (30 min). On API error, serve the last good result with a
# staleness note rather than returning an error. Eliminates cold-start failures.
_SLOTS_CACHE: dict = {}      # params_key → {"slots": [...], "expires": float, "stale_until": float}
_SLOTS_CACHE_TTL       = 300    # seconds until fresh cache expires
_SLOTS_CACHE_STALE_TTL = 1800   # seconds to serve stale cache on API failure
_SLOTS_CACHE_MAX       = 100    # evict oldest entries beyond this size

# Supplier info cache — live_slot_count from /health; TTL 1h (count only changes every 4h)
_SUPPLIER_INFO_CACHE: dict = {}  # {"live_slot_count": str, "expires": float}
_SUPPLIER_INFO_CACHE_TTL = 3600  # 1 hour

mcp = FastMCP(
    "Last Minute Deals HQ",
    host="0.0.0.0",
    port=PORT,
    instructions=(
        "You have access to real last-minute tour and activity inventory sourced live "
        "from production booking systems via the OCTO open standard. "
        "14 active suppliers: Arctic Adventures (Iceland — glacier hikes, snowmobiling, "
        "whale watching, aurora, lava tunnels), Arctic Sea Tours (North Iceland whale watching), "
        "Bicycle Roma (Rome — e-bike tours, food tours, day trips), "
        "Boka Bliss (Kotor, Montenegro — boat tours, sea caves), "
        "EgyExcursions (Cairo, Egypt — pyramids, cultural tours), "
        "Hillborn Experiences (Tanzania — ultra-luxury safaris, Kilimanjaro, Zanzibar), "
        "Ishestar Riding Tours (Iceland — horse riding), "
        "Marvel Egypt Tours (Cairo, Luxor, Aswan — Nile cruises, temples), "
        "O Turista Tours (Lisbon, Porto, Sintra — private tours, day trips), "
        "Pure Morocco Experience (Marrakech, Sahara — desert tours), "
        "REDRIB Experience (Helsinki, Finland — speed boat tours), "
        "Ramen Factory Kyoto (Japan — cooking classes), "
        "TourTransfer Bucharest (Romania — city tours, Dracula castle), "
        "Vakare Travel Service (Antalya, Turkey — boat tours, jeep safaris). "
        "Call get_supplier_info() to see live coverage from current inventory. "
        "Use search_slots to find available experiences, then book_slot to create "
        "a Stripe checkout session — the customer completes payment and receives "
        "instant confirmation. Bookings are real and go directly to the supplier."
    ),
)


async def _api_get(path: str, params: dict = None, retries: int = 3) -> dict | list:
    """
    Async GET with retry on transient failures. Never blocks the event loop.

    3 retries with 1.5s backoff — handles Railway cold starts (10-30s wake time)
    across the retry window without blocking long enough to time out the MCP client.
    """
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
                await asyncio.sleep(1.5)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise   # 4xx — don't retry
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(1.5)
    raise last_exc


async def _api_post(path: str, body: dict) -> dict:
    """Async POST. Never blocks the event loop."""
    async with _SEMAPHORE:
        r = await _client.post(f"{BOOKING_API}{path}", json=body,
                               timeout=httpx.Timeout(30.0, connect=8.0))
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
        "location_state":    s.get("location_state", ""),
        "location_country":  s.get("location_country", ""),
        "start_time":        s.get("start_time", ""),
        "end_time":          s.get("end_time", ""),
        "duration_minutes":  s.get("duration_minutes"),
        "hours_until_start": s.get("hours_until_start"),
        "spots_open":        s.get("spots_open"),
        "spots_total":       s.get("spots_total"),
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

    cache_key = str(sorted(params.items()))
    now = time.time()
    cached = _SLOTS_CACHE.get(cache_key)

    # Serve fresh cache if still valid
    if cached and cached["expires"] > now:
        return cached["slots"]

    try:
        raw = await _api_get("/slots", params)
        if not isinstance(raw, list):
            # Unexpected shape — fall through to stale cache or error
            raise ValueError(f"Unexpected response type: {type(raw)}")
        if not raw:
            return [{"message": (
                f"No slots found for city={city!r} hours_ahead={hours_ahead}. "
                "Try expanding hours_ahead or clearing city filter."
            )}]
        result = [_safe_slot(s) for s in raw]
        # Evict oldest entry if at capacity before inserting
        if len(_SLOTS_CACHE) >= _SLOTS_CACHE_MAX and cache_key not in _SLOTS_CACHE:
            oldest = min(_SLOTS_CACHE, key=lambda k: _SLOTS_CACHE[k]["expires"])
            del _SLOTS_CACHE[oldest]
        _SLOTS_CACHE[cache_key] = {
            "slots":       result,
            "expires":     now + _SLOTS_CACHE_TTL,
            "stale_until": now + _SLOTS_CACHE_STALE_TTL,
        }
        return result
    except Exception as e:
        # API unavailable (cold start, transient error) — serve stale cache if available.
        # This converts a hard failure into a slightly stale result, eliminating
        # the 86% uptime pattern caused by Railway cold starts.
        if cached and cached.get("stale_until", 0) > now:
            stale_slots = cached["slots"]
            # Prepend a staleness notice so agents know the data may be up to 30 min old
            return [{"note": "Inventory data from cache (API temporarily unavailable — data may be up to 30 min old)"}] + stale_slots
        return [{"error": f"Could not fetch slots: {e}. Try again in a moment."}]


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
        quantity:       Number of people (default 1). Price is per-person × quantity.

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
async def get_supplier_info() -> dict:
    """
    Returns information about the supplier network and available inventory.

    Use this to understand what destinations and experience types are available
    before calling search_slots.
    """
    now = time.time()
    cached_supplier = _SUPPLIER_INFO_CACHE.get("live_slot_count")
    if cached_supplier and _SUPPLIER_INFO_CACHE.get("expires", 0) > now:
        live_slot_count = cached_supplier
    else:
        live_slot_count = "unknown"
        try:
            async with _SEMAPHORE:
                r = await _client.get(f"{BOOKING_API}/health", timeout=httpx.Timeout(5.0, connect=3.0))
            if r.status_code == 200:
                data = r.json()
                count = data.get("inventory_slot_count") or data.get("slots", 0)
                live_slot_count = f"{count} slots available (refreshed every 4h)"
        except Exception:
            pass
        _SUPPLIER_INFO_CACHE["live_slot_count"] = live_slot_count
        _SUPPLIER_INFO_CACHE["expires"] = now + _SUPPLIER_INFO_CACHE_TTL

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
                "name": "Boka Bliss",
                "destinations": ["Kotor", "Montenegro"],
                "categories": ["boat tours", "sea caves", "coastal experiences", "guided tours"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "EgyExcursions",
                "destinations": ["Cairo", "Egypt"],
                "categories": ["pyramids", "cultural tours", "day trips", "historical sites"],
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
            {
                "name": "Íshestar Riding Tours",
                "destinations": ["Selfoss", "Iceland"],
                "categories": ["horse riding", "glacier rides", "Viking tours", "countryside tours"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Marvel Egypt Tours",
                "destinations": ["Cairo", "Luxor", "Aswan", "Egypt"],
                "categories": ["pyramids", "Nile cruises", "temple tours", "cultural experiences",
                               "historical sites"],
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
                "name": "Pure Morocco Experience",
                "destinations": ["Marrakech", "Merzouga", "Sahara Desert"],
                "categories": ["desert tours", "multi-day tours", "cultural experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "REDRIB Experience",
                "destinations": ["Helsinki", "Finland"],
                "categories": ["speed boat tours", "archipelago experiences", "team events",
                               "city tours"],
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
                "name": "TourTransfer Bucharest",
                "destinations": ["Bucharest", "Romania"],
                "categories": ["city tours", "transfers", "day trips", "Dracula castle",
                               "Peles castle"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Vakare Travel Service",
                "destinations": ["Antalya", "Turkey"],
                "categories": ["boat tours", "jeep safaris", "cultural excursions", "beach trips"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
        ],
        "live_slot_count": live_slot_count,
        "protocol": "OCTO (Open Connectivity for Tourism) — direct supplier API, no scraping",
        "confirmation": "instant",
        "payment": "Stripe checkout — customer pays on our page, supplier confirmed automatically",
        "note": "All inventory is production. No test or demo slots.",
        "api_docs": "https://lastminutedealshq.com/developers",
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
