"""
run_mcp_remote.py — Hosted remote MCP server for Last Minute Deals HQ.

Exposes last-minute tour/activity inventory as MCP tools via Streamable HTTP transport.
Runs on Railway at https://api.lastminutedealshq.com

Unlike run_mcp_server.py (which reads local .tmp/ files), this server
calls the Railway booking API directly — no local setup required.

── Claude Desktop config ────────────────────────────────────────────────────
Add to %APPDATA%\\Claude\\claude_desktop_config.json:

{
  "mcpServers": {
    "lastminutedeals": {
      "url": "https://api.lastminutedealshq.com/mcp"
    }
  }
}

── Claude Code config ───────────────────────────────────────────────────────
claude mcp add lastminutedeals --url https://api.lastminutedealshq.com/mcp

── Environment variables ────────────────────────────────────────────────────
  BOOKING_API_URL        — Railway API base URL (e.g. https://api.lastminutedealshq.com)
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

BOOKING_API = (os.getenv("BOOKING_API_URL") or "https://api.lastminutedealshq.com").rstrip("/")
API_KEY     = os.getenv("LMD_WEBSITE_API_KEY", "")
HDRS        = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
PORT        = int(os.getenv("PORT", "8080"))

# Shared async HTTP client — connection pool, keep-alive, no blocking.
# connect=8s: gives Railway time to wake from sleep (cold starts take 10-30s but
# 8s covers most cases; the retry loop provides two more chances after that).
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_client  = httpx.AsyncClient(headers=HDRS, timeout=_TIMEOUT)

# Keep-alive ping — Railway free tier sleeps containers after 15 min idle; a cold
# start takes 10-30s and exhausts our 3×8s retry window, causing ~25% of search_slots
# calls to fail. Pinging /health every 10 min keeps the container warm.
_WARMUP_TASK: asyncio.Task | None = None
_WARMUP_INTERVAL_S = 600  # 10 minutes


# Concurrency guard — cap simultaneous outbound API calls to prevent
# request storms from hammering the booking API or Supabase under burst load.
_SEMAPHORE = asyncio.Semaphore(10)


async def _keep_railway_warm() -> None:
    while True:
        await asyncio.sleep(_WARMUP_INTERVAL_S)
        try:
            async with _SEMAPHORE:
                await _client.get(f"{BOOKING_API}/health",
                                  timeout=httpx.Timeout(10.0, connect=8.0))
        except Exception:
            pass  # Warm-up ping; ignore failures — main retry logic covers transients

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
    stateless_http=True,
    instructions=(
        "You have access to real last-minute tour and activity inventory sourced live "
        "from production booking systems via the OCTO open standard. "
        "23 active suppliers: Adi Tours - Nuba travel (Cairo, Egypt — pyramids, desert tours), "
        "All Washington View (Washington D.C. — city tours, sightseeing), "
        "Arctic Adventures (Iceland — glacier hikes, snowmobiling, whale watching, aurora, lava tunnels), "
        "Bicycle Roma (Rome — e-bike tours, food tours, day trips), "
        "Boka Bliss (Kotor, Montenegro — boat tours, sea caves), "
        "EgyExcursions (Cairo, Egypt — pyramids, cultural tours), "
        "Hillborn Experiences (Tanzania — ultra-luxury safaris, Kilimanjaro, Zanzibar), "
        "Ishestar Riding Tours (Iceland — horse riding), "
        "Marvel Egypt Tours (Cairo, Luxor, Aswan — Nile cruises, temples), "
        "Nefertiti Tours (Cairo, Giza — pyramids, camel rides, ATV desert tours), "
        "O Turista Tours (Lisbon, Porto, Sintra — private tours, day trips), "
        "Perfect Day Tours (Luxor, Egypt — hot air balloon, temples, horse carriage tours), "
        "Pure Morocco Experience (Marrakech, Sahara — desert tours), "
        "REDRIB Experience (Helsinki, Finland — speed boat tours), "
        "Ramen Factory Kyoto (Japan — cooking classes), "
        "Sailing Windermere (Windermere, UK — sailing experiences on Lake Windermere), "
        "The Photo Experience (London — photography tours), "
        "TourTransfer Bucharest (Romania — city tours, Dracula castle), "
        "Tours El Chiquiz (Puerto Vallarta, Mexico — tequila tasting, hiking), "
        "Trivanzo Holidays (Egypt — Nile cruises, Red Sea, cultural tours), "
        "TUTU VIEW Ltd (China — multi-day tours, silk road, cultural experiences), "
        "Vakare Travel Service (Antalya, Turkey — boat tours, jeep safaris), "
        "Zestro Bizlinks (Japan — experiences). "
        "BOOKING WORKFLOW — follow this sequence every time a user wants to book: "
        "1. Call search_slots with the user's city/destination and preferred timeframe. "
        "2. Present options and get the user's selection. "
        "3. Call preview_slot(slot_id) to get a booking page URL. "
        "4. Share the booking_page_url with the user — they click it, enter their details, "
        "and pay via Stripe. No need to collect name/email/phone yourself. "
        "5. If the user prefers, you can instead collect their name, email, and phone "
        "and call book_slot directly — then share the checkout_url immediately. "
        "6. Call get_booking_status to confirm once payment is complete. "
        "AUTONOMOUS MODE: if you have a wallet_id, pass it with execution_mode='autonomous' "
        "to skip the checkout step entirely — booking completes immediately with a "
        "confirmation number. Call get_supplier_info() to see live destination coverage."
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
    hours_ahead: float = 168.0,
    max_price: float = 0.0,
    limit: int = 50,
) -> list[dict]:
    """
    Search for last-minute available tours and activities.

    Returns real production inventory from 23 suppliers (Adi Tours - Nuba travel, All Washington View,
    Arctic Adventures, Bicycle Roma, Boka Bliss, EgyExcursions,
    Hillborn Experiences, Íshestar Riding Tours, Marvel Egypt Tours, Nefertiti Tours,
    O Turista Tours, Perfect Day Tours, Pure Morocco Experience, REDRIB Experience,
    Ramen Factory Kyoto, Sailing Windermere, The Photo Experience, TourTransfer Bucharest,
    Tours El Chiquiz, Trivanzo Holidays, TUTU VIEW Ltd, Vakare Travel Service, Zestro Bizlinks)
    sourced live via the OCTO open booking protocol.
    Slots are sorted by urgency (soonest first).

    Args:
        city:        City or country filter, partial match (e.g. "Reykjavik", "Rome", "Iceland").
                     Leave empty to search all locations.
        category:    Category filter. Use "experiences" for tours/activities.
                     Leave empty for all categories.
        hours_ahead: Return slots starting within this many hours (default: 168).
        max_price:   Maximum price in USD. Set to 0 to return all prices.
        limit:       Max results to return (default 50). Results are sorted by urgency
                     so the most time-sensitive slots come first. Increase for broader
                     browsing (e.g. limit=500). Use city/category filters to narrow
                     results instead of raising the limit when possible.

    Returns:
        List of available slot dicts sorted by hours_until_start (soonest first).
    """
    # Start keep-alive task on first invocation — prevents Railway container sleep
    # (cold starts exhaust the retry window and cause tool failures).
    global _WARMUP_TASK
    if _WARMUP_TASK is None or _WARMUP_TASK.done():
        _WARMUP_TASK = asyncio.create_task(_keep_railway_warm())

    # hours_ahead must be int: Flask's type=int silently returns the default (168h)
    # when given a float string like "72.0", so the time-window filter is dropped.
    # Default 50 keeps responses small (~30KB) for fast proxy transit.
    # No hard cap — agents can request more when filtering by city/category.
    # The 30% failure rate was caused by having NO default (2400 slots, 1.4MB per call).
    safe_limit = max(1, int(limit))
    params: dict = {"hours_ahead": int(hours_ahead), "limit": safe_limit}
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
        result = [_safe_slot(s) for s in raw[:safe_limit]]
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
    wallet_id: str = "",
    execution_mode: str = "approval",
) -> dict:
    """
    Book a last-minute slot for a customer. Two modes:

    APPROVAL MODE (default — no wallet_id):
        Creates a Stripe Checkout Session and returns a checkout_url.
        You MUST share this URL with the customer immediately — do not summarise it,
        do not wait, show it directly so they can complete payment.
        The booking is confirmed with the supplier after payment succeeds.
        The session expires in 24 hours.

    AUTONOMOUS MODE (wallet_id + execution_mode='autonomous'):
        The booking completes immediately using a pre-funded agent wallet.
        Returns a confirmation_number directly — no checkout step, no human action needed.
        Use this when your application manages payment on behalf of the customer.

    Args:
        slot_id:        Slot ID from search_slots results.
        customer_name:  Full name of the person attending.
        customer_email: Email address for booking confirmation.
        customer_phone: Phone number including country code (e.g. +15550001234).
        quantity:       Number of people (default 1). Price is per-person × quantity.
        wallet_id:      Pre-funded agent wallet ID (format: wlt_...). Enables autonomous mode.
        execution_mode: Set to 'autonomous' when providing a wallet_id.

    Returns:
        Approval mode:   { success: true, checkout_url, booking_id, expires_at, action_required }
        Autonomous mode: { success: true, confirmation_number, booking_id, status: 'booked' }
        On error:        { success: false, error }
    """
    try:
        payload: dict = {
            "slot_id":        slot_id,
            "customer_name":  customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "quantity":       max(1, int(quantity)),
        }
        if wallet_id:
            payload["wallet_id"] = wallet_id
        if execution_mode:
            payload["execution_mode"] = execution_mode
        return await _api_post("/api/book", payload)
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
        Booking record with status, confirmation_number, service details, and checkout_url.
        Status values:
          pending_payment — awaiting customer checkout
          fulfilling      — payment received, confirming with supplier (up to 45s)
          booked          — confirmed by supplier; confirmation_number is set
          failed          — fulfillment failed; payment hold cancelled
          cancelled       — booking cancelled and refunded
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
async def preview_slot(slot_id: str) -> dict:
    """
    Get a shareable booking page URL for a slot.

    Returns a link the user can open in their browser to see full details
    (service name, date/time, price, location) and complete the booking
    themselves — they enter their own name, email, and phone on the page
    and pay via Stripe.

    Use this instead of book_slot when the user is a human browsing with an
    AI assistant. No need to collect customer details yourself — the booking
    page handles everything.

    Args:
        slot_id: Slot ID from search_slots results.

    Returns:
        booking_page_url, service name, price, start time, and location.
    """
    try:
        data = await _api_get(f"/slots/{slot_id}/quote")
    except Exception:
        data = None
    if not data or not data.get("available"):
        return {"error": "Slot not found or no longer available."}
    host = BOOKING_API.replace("web-production-dc74b.up.railway.app", "api.lastminutedealshq.com")  # no-op when BOOKING_API already uses custom domain
    return {
        "booking_page_url": f"{host}/book/{slot_id}",
        "service_name": data.get("service_name", ""),
        "business_name": data.get("business_name", ""),
        "start_time": data.get("start_time", ""),
        "location_city": data.get("location_city", ""),
        "price": float(data.get("our_price") or data.get("price") or 0),
        "currency": data.get("currency", "USD"),
        "instructions": "Share the booking_page_url with the user. They can view details and complete the booking themselves.",
    }


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
                "name": "Trivanzo Holidays",
                "destinations": ["Cairo", "Luxor", "Aswan", "Red Sea", "Egypt"],
                "categories": ["Nile cruises", "cultural tours", "Red Sea excursions",
                               "desert tours", "day trips"],
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
            {
                "name": "All Washington View",
                "destinations": ["Washington, D.C.", "United States"],
                "categories": ["city tours", "sightseeing", "monuments", "panoramic views"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "TUTU VIEW Ltd",
                "destinations": ["China", "Shanghai", "Xi'an", "Beijing", "Chengdu", "Hangzhou", "Chongqing", "Shenzhen", "Changsha"],
                "categories": ["multi-day tours", "cultural experiences", "silk road", "food tours", "nature tours"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Tours El Chiquiz",
                "destinations": ["Puerto Vallarta", "Mexico"],
                "categories": ["tours", "excursions", "cultural experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Zestro Bizlinks",
                "destinations": ["Japan"],
                "categories": ["experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "Adi Tours - Nuba travel",
                "destinations": ["Cairo", "Egypt"],
                "categories": ["experiences"],
                "booking_platform": "Bokun",
                "confirmation": "instant",
            },
            {
                "name": "The Photo Experience",
                "destinations": ["London", "United Kingdom"],
                "categories": ["experiences"],
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


@mcp.prompt()
def find_experiences(city: str, hours_ahead: str = "72") -> str:
    """
    Search for last-minute tours and activities in a specific destination.

    Args:
        city:        City or country to search (e.g. "Reykjavik", "Rome", "Egypt").
        hours_ahead: How soon the slot must start — in hours (default: 72).
    """
    return (
        f"Find me last-minute experience slots available in {city} "
        f"within the next {hours_ahead} hours. "
        "Call search_slots with that city and hours_ahead value. "
        "Show me the results — service name, start time, price, and duration — "
        "then ask which one I'd like to book. "
        "Once I choose, call preview_slot to get a booking page link and share it with me "
        "so I can enter my details and pay directly."
    )


@mcp.prompt()
def explore_destinations() -> str:
    """
    See all available destinations and experience types before searching.
    Use this to understand what's available across the supplier network.
    """
    return (
        "Call get_supplier_info and show me all available destinations "
        "and experience types. Group by region (Europe, Middle East/Africa, Asia). "
        "After showing the overview, ask which destination interests me so we can "
        "search for specific last-minute slots."
    )


@mcp.prompt()
def autonomous_booking(wallet_id: str, city: str = "", category: str = "") -> str:
    """
    Book a last-minute slot using a pre-funded agent wallet — no checkout required.

    Args:
        wallet_id: Pre-funded wallet ID (format: wlt_...).
        city:      Optional city/destination to filter by.
        category:  Optional category filter (e.g. 'experiences', 'wellness').
    """
    city_part     = f" in {city}" if city else ""
    category_part = f" in category '{category}'" if category else ""
    return (
        f"I have a pre-funded wallet (wallet_id: {wallet_id}). "
        f"Search for available last-minute slots{city_part}{category_part} "
        "using search_slots. Show me the top 5 options with price and timing. "
        "Once I pick one, collect my name, email, and phone number, then call "
        f"book_slot with wallet_id='{wallet_id}' and execution_mode='autonomous'. "
        "Return the confirmation_number directly — no checkout step needed."
    )


if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run()  # stdio mode for mcp-proxy (Glama)
    else:
        mcp.run(transport="streamable-http")
