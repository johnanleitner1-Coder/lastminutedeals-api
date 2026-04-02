"""
lmd_sdk.py — LastMinuteDeals Python SDK

One-file, zero-dependency SDK (uses stdlib urllib only).
Drop this file anywhere in your project and import it.

Quick start:
    from lmd_sdk import LastMinuteDeals

    lmd = LastMinuteDeals(api_key="lmd_...")  # get key at POST /api/keys/register

    # Search
    slots = lmd.search(city="New York", category="wellness", hours_ahead=24)

    # Book (checkout redirect — user completes payment)
    result = lmd.book(slot_id=slots[0]["slot_id"], customer={"name": "...", "email": "...", "phone": "..."})

    # Guaranteed booking (wallet — no redirect, synchronous outcome)
    result = lmd.execute(category="wellness", city="NYC", wallet_id="wlt_...", customer={...})

    # Delegated intent (fire and forget — system works until done)
    intent = lmd.intent(category="wellness", city="NYC", wallet_id="wlt_...", customer={...},
                        callback_url="https://your-agent.com/webhook")

    # Market intelligence
    data = lmd.insights(category="wellness", city="NYC")

    # Performance metrics (no auth)
    metrics = lmd.metrics()

See full API spec at https://lastminutedealshq.com/openapi.json
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# Default API base — override if self-hosting
DEFAULT_API_BASE = "https://api.lastminutedealshq.com"


class LastMinuteDealsError(Exception):
    """Raised when the API returns an error response."""
    def __init__(self, message: str, status_code: int = 0, response: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response    = response or {}


class LastMinuteDeals:
    """
    LastMinuteDeals API client.

    All methods return parsed JSON as Python dicts/lists.
    Raises LastMinuteDealsError on API errors.
    """

    def __init__(self, api_key: str = "", base_url: str = DEFAULT_API_BASE, timeout: int = 30):
        self.api_key  = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _headers(self, require_auth: bool = True) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        elif require_auth:
            raise LastMinuteDealsError(
                "API key required. Register at POST /api/keys/register or pass api_key= to LastMinuteDeals()."
            )
        return h

    def _get(self, path: str, params: dict | None = None, auth: bool = False) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers=self._headers(require_auth=auth))
        return self._send(req)

    def _post(self, path: str, body: dict, auth: bool = True) -> Any:
        url  = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        req  = urllib.request.Request(url, data=data, headers=self._headers(require_auth=auth), method="POST")
        return self._send(req)

    def _send(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"raw": body}
            msg = err_data.get("error") or err_data.get("message") or f"HTTP {e.code}"
            raise LastMinuteDealsError(msg, status_code=e.code, response=err_data)

    # ── Registration ──────────────────────────────────────────────────────────

    @classmethod
    def register(cls, name: str, email: str, base_url: str = DEFAULT_API_BASE) -> "LastMinuteDeals":
        """
        Register for a free API key and return a configured client.

        Usage:
            lmd = LastMinuteDeals.register("MyAgent", "agent@example.com")
            print(lmd.api_key)  # save this
        """
        client = cls(base_url=base_url)
        result = client._post("/api/keys/register", {"name": name, "email": email}, auth=False)
        key = result.get("api_key", "")
        if not key:
            raise LastMinuteDealsError("Registration failed: no API key returned.", response=result)
        client.api_key = key
        return client

    # ── Slot search ───────────────────────────────────────────────────────────

    def search(
        self,
        category: str = "",
        city: str = "",
        hours_ahead: int = 72,
        max_price: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Search available last-minute slots.

        Returns a list of slot dicts sorted by hours_until_start (soonest first).
        No auth required.

        Args:
            category:    "wellness" | "beauty" | "hospitality" | "entertainment" | etc.
            city:        City name, e.g. "New York" or "NYC"
            hours_ahead: Only return slots starting within this many hours (max 72)
            max_price:   Filter to slots at or below this price
            limit:       Max results (default 50, max 500)

        Example:
            slots = lmd.search(city="Chicago", category="wellness", hours_ahead=24, max_price=100)
        """
        return self._get("/slots", {
            "category":    category or None,
            "city":        city or None,
            "hours_ahead": hours_ahead,
            "max_price":   max_price,
            "limit":       limit,
        }, auth=False)

    def quote(self, slot_id: str) -> dict:
        """
        Confirm a slot is still available and get the current price.

        Returns: { "available": bool, "our_price": float, "start_time": "...", ... }
        """
        return self._get(f"/slots/{urllib.parse.quote(slot_id)}/quote", auth=False)

    # ── Booking ───────────────────────────────────────────────────────────────

    def book(self, slot_id: str, customer: dict) -> dict:
        """
        Initiate a booking via Stripe Checkout (user redirected to pay).
        Card is only captured after the booking is confirmed on the source platform.

        Args:
            slot_id:  From search() results
            customer: {"name": "...", "email": "...", "phone": "..."}

        Returns: {"success": True, "checkout_url": "https://checkout.stripe.com/..."}
        Redirect user to checkout_url to complete payment.
        """
        return self._post("/api/book", {
            "slot_id":        slot_id,
            "customer_name":  customer.get("name", ""),
            "customer_email": customer.get("email", ""),
            "customer_phone": customer.get("phone", ""),
        })

    # ── Guaranteed execution ──────────────────────────────────────────────────

    def execute(
        self,
        customer: dict,
        slot_id: str = "",
        category: str = "",
        city: str = "",
        hours_ahead: int = 24,
        budget: float | None = None,
        allow_alternatives: bool = True,
        wallet_id: str = "",
        payment_intent_id: str = "",
    ) -> dict:
        """
        Guaranteed booking — synchronous, multi-path retry, hard outcome.
        Returns only when the outcome is known: booked or failed.
        Requires either wallet_id (pre-funded) or payment_intent_id (Stripe hold).

        Up to 7 strategies tried automatically:
        original slot → retry → similar → different platform → metro area → alternatives

        Args:
            customer:  {"name": "...", "email": "...", "phone": "..."}
            slot_id:   Optional preferred slot (engine falls back if unavailable)
            category:  Category filter
            city:      City filter
            wallet_id: Pre-funded wallet (fastest — no per-booking Stripe roundtrip)
            payment_intent_id: Existing Stripe PaymentIntent (capture on success)

        Returns:
            {
                "success": True,
                "status":  "booked",
                "confirmation": "EVT-12345",
                "attempts":  2,
                "fallbacks_used": 1,
                "savings_vs_market": 15.00,
                "confidence_score": 0.75,
                "attempt_log": [...]
            }
        """
        body: dict = {
            "customer":           customer,
            "hours_ahead":        hours_ahead,
            "allow_alternatives": allow_alternatives,
        }
        if slot_id:             body["slot_id"]            = slot_id
        if category:            body["category"]           = category
        if city:                body["city"]               = city
        if budget is not None:  body["budget"]             = budget
        if wallet_id:           body["wallet_id"]          = wallet_id
        if payment_intent_id:   body["payment_intent_id"]  = payment_intent_id

        return self._post("/execute/guaranteed", body)

    def best(
        self,
        customer: dict,
        goal: str = "maximize_value",
        city: str = "",
        category: str = "",
        budget: float | None = None,
        hours_ahead: int = 48,
        wallet_id: str = "",
        payment_intent_id: str = "",
        explain: bool = False,
    ) -> dict:
        """
        Goal-oriented booking — tell us what you want, we decide what to book.

        Goals:
            "maximize_value"   — best discount vs market rate
            "minimize_wait"    — soonest available slot
            "maximize_success" — highest platform reliability score
            "minimize_price"   — cheapest absolute price within budget

        Args:
            explain: If True, response includes reasoning for why this slot was chosen.

        Example:
            result = lmd.best(
                goal="maximize_value",
                city="Detroit",
                budget=150,
                customer={"name": "...", "email": "...", "phone": "..."},
                wallet_id="wlt_...",
            )
        """
        body: dict = {
            "goal":        goal,
            "customer":    customer,
            "hours_ahead": hours_ahead,
            "explain":     explain,
        }
        if city:                body["city"]              = city
        if category:            body["category"]          = category
        if budget is not None:  body["budget"]            = budget
        if wallet_id:           body["wallet_id"]         = wallet_id
        if payment_intent_id:   body["payment_intent_id"] = payment_intent_id

        return self._post("/execute/best", body)

    # ── Intent sessions ───────────────────────────────────────────────────────

    def intent(
        self,
        customer: dict,
        goal: str = "find_and_book",
        category: str = "",
        city: str = "",
        budget: float | None = None,
        hours_ahead: int = 48,
        allow_alternatives: bool = True,
        wallet_id: str = "",
        autonomy: str = "full",
        callback_url: str = "",
        ttl_hours: int = 24,
        price_target: float | None = None,
    ) -> dict:
        """
        Create a persistent intent — the system works on your goal until done.

        Fire and forget: create an intent, set a callback_url, and get notified
        when the booking completes. No polling needed.

        Autonomy modes:
            "full"    — auto-execute when matching slots appear (default)
            "notify"  — send callback first, wait for /intent/{id}/execute
            "monitor" — observe only, never execute

        Goal types:
            "find_and_book"  — monitor + execute (requires customer + payment)
            "monitor_only"   — notify when slots appear, no booking
            "price_alert"    — notify when price drops below price_target

        Returns: {"intent_id": "int_...", "status": "monitoring", "expires_at": "..."}

        Check status:  lmd.intent_status(intent_id)
        Cancel:        lmd.intent_cancel(intent_id)
        Manual exec:   lmd.intent_execute(intent_id)
        """
        constraints: dict = {"hours_ahead": hours_ahead, "allow_alternatives": allow_alternatives}
        if category:            constraints["category"]     = category
        if city:                constraints["city"]         = city
        if budget is not None:  constraints["budget"]       = budget
        if price_target:        constraints["price_target"] = price_target

        payment: dict = {}
        if wallet_id:
            payment = {"method": "wallet", "wallet_id": wallet_id}

        body: dict = {
            "goal":        goal,
            "constraints": constraints,
            "customer":    customer,
            "payment":     payment,
            "autonomy":    autonomy,
            "ttl_hours":   ttl_hours,
        }
        if callback_url:
            body["callback_url"] = callback_url

        return self._post("/intent/create", body)

    def intent_status(self, intent_id: str) -> dict:
        """Get current status and result of an intent session."""
        return self._get(f"/intent/{urllib.parse.quote(intent_id)}")

    def intent_execute(self, intent_id: str) -> dict:
        """Manually trigger a 'notify' autonomy intent."""
        return self._post(f"/intent/{urllib.parse.quote(intent_id)}/execute", {})

    def intent_cancel(self, intent_id: str) -> dict:
        """Cancel an active intent session."""
        return self._post(f"/intent/{urllib.parse.quote(intent_id)}/cancel", {})

    def intent_list(self) -> list[dict]:
        """List all your active intent sessions."""
        return self._get("/intent/list")

    # ── Wallets ───────────────────────────────────────────────────────────────

    def wallet_create(self, name: str, email: str) -> dict:
        """
        Create a pre-funded agent wallet. Returns wallet_id and api_key.
        Fund it once, book many times with no per-booking Stripe redirect.

        Returns: {"wallet_id": "wlt_...", "api_key": "lmd_...", "balance": 0.0}
        """
        return self._post("/api/wallets/create", {"name": name, "email": email}, auth=False)

    def wallet_fund(self, wallet_id: str, amount_dollars: float) -> dict:
        """
        Get a Stripe Checkout link to add funds to a wallet.
        Returns: {"success": True, "checkout_url": "https://checkout.stripe.com/..."}
        """
        return self._post("/api/wallets/fund", {"wallet_id": wallet_id, "amount_dollars": amount_dollars}, auth=False)

    def wallet_balance(self, wallet_id: str) -> dict:
        """Check wallet balance. Returns {"balance": 42.00, "currency": "usd"}"""
        return self._get(f"/api/wallets/{urllib.parse.quote(wallet_id)}/balance")

    # ── Market intelligence ───────────────────────────────────────────────────

    def insights(self, category: str = "", city: str = "") -> dict:
        """
        Market intelligence: platform success rates, fill velocity, optimal
        booking windows, live inventory. Data compounds over time.

        No auth required — intentionally public.
        """
        return self._get("/insights/market", {"category": category or None, "city": city or None}, auth=False)

    def metrics(self) -> dict:
        """
        Public performance metrics: success rate, slot count, data freshness,
        active intents. No auth required.
        """
        return self._get("/metrics", auth=False)

    # ── MCP tool interface ────────────────────────────────────────────────────

    def mcp(self, tool: str, **arguments) -> Any:
        """
        Call an MCP tool via HTTP (no special transport required).

        Available tools:
            search_last_minute_slots(category, city, hours_ahead, max_price, limit)
            get_slot_details(slot_id)
            book_slot(slot_id, customer_name, customer_email, customer_phone)
            get_booking_status(booking_id)

        Example:
            result = lmd.mcp("search_last_minute_slots", city="NYC", category="wellness")
        """
        return self._post("/mcp", {"tool": tool, "arguments": arguments}, auth=False)
