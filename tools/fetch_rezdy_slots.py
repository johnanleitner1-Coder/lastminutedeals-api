"""
fetch_rezdy_slots.py — Fetch last-minute availability via the Rezdy Agent API.

Rezdy is a booking platform for tours and activities. The Agent API is free
(create account at rezdy.com, select Reseller, request API key via Integrations
menu — approved in ~48 hours). Suppliers individually grant agents access to
their rates, so inventory builds as you establish relationships.

Usage:
    python tools/fetch_rezdy_slots.py [--hours-ahead 72] [--staging] [--limit 100]

Output:
    .tmp/rezdy_slots.json  — normalized slot records

── Getting started ─────────────────────────────────────────────────────────────

1. Create free account at rezdy.com (select "Reseller" during signup)
2. Go to Integrations menu → request an API key
3. API key is provisioned in ~48 hours
4. Add to .env:  REZDY_API_KEY=<your_key>
5. Run:  python tools/fetch_rezdy_slots.py

Test in staging first:
    python tools/fetch_rezdy_slots.py --staging

Note on inventory access:
    On a new account, few suppliers will have granted you rate access. As you
    establish Rezdy supplier relationships (via the marketplace), inventory grows.
    Initially, test using Rezdy's built-in "Rezdy Agent Certification" test supplier.

── Rezdy Agent API reference ───────────────────────────────────────────────────
  GET  /products               — list all products you have access to
  GET  /availability           — get sessions for a product in a date range
  POST /bookings               — create a booking
  Auth: ?apiKey={key} query param (not Bearer token)
  Docs: https://developers.rezdy.com/rezdyapi/index-agent.html
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from normalize_slot import normalize, compute_slot_id

BASE_DIR    = Path(__file__).parent.parent
TMP_DIR     = BASE_DIR / ".tmp"
OUTPUT_FILE = TMP_DIR / "rezdy_slots.json"

REZDY_API_BASE    = "https://api.rezdy.com/v1"
REZDY_STAGING_BASE = "https://api.rezdy-staging.com/v1"
REQUEST_DELAY_S   = 0.4

# Category keyword mapping — infer from product name / productType
CATEGORY_MAP = {
    "yoga":        "wellness",
    "pilates":     "wellness",
    "fitness":     "wellness",
    "meditation":  "wellness",
    "spa":         "wellness",
    "massage":     "wellness",
    "salon":       "beauty",
    "beauty":      "beauty",
    "hair":        "beauty",
    "nail":        "beauty",
}


# ── Normalization helpers ─────────────────────────────────────────────────────

def _parse_rezdy_datetime(dt_str: str) -> datetime | None:
    """Parse a Rezdy localDateTimeStart (YYYY-MM-DD HH:MM:SS) to UTC-aware datetime."""
    if not dt_str:
        return None
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        # Rezdy times are local to the supplier — treat as UTC for now.
        # TODO: use supplier timezone if available in product data.
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _infer_category(product: dict) -> str:
    name = (product.get("name") or product.get("shortDescription") or "").lower()
    ptype = (product.get("productType") or "").lower()
    text = f"{name} {ptype}"
    for kw, cat in CATEGORY_MAP.items():
        if kw in text:
            return cat
    return "experiences"


def _extract_price(price_options: list[dict]) -> float | None:
    """Return the retail price of the first available price option, in dollars."""
    if not price_options:
        return None
    for opt in price_options:
        price = opt.get("price")
        if price is not None:
            try:
                return round(float(price), 2)
            except (ValueError, TypeError):
                continue
    return None


def session_to_slot(session: dict, product: dict) -> dict | None:
    """Convert a Rezdy session (availability record) to a normalized slot."""
    start_str = session.get("startTimeLocal") or session.get("startTime", "")
    end_str   = session.get("endTimeLocal") or session.get("endTime", "")

    start_dt = _parse_rezdy_datetime(start_str)
    end_dt   = _parse_rezdy_datetime(end_str)

    if not start_dt:
        return None

    now = datetime.now(timezone.utc)
    hours_until = (start_dt - now).total_seconds() / 3600

    if hours_until < 0 or hours_until > 72:
        return None

    seats_available = session.get("seatsAvailable")
    seats_total     = session.get("seats")

    if seats_available is not None and seats_available <= 0:
        return None   # sold out

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if end_dt else None

    duration_min = None
    if start_dt and end_dt:
        duration_min = int((end_dt - start_dt).total_seconds() / 60)

    product_code = product.get("productCode", "")
    product_name = product.get("name") or product_code
    price        = _extract_price(session.get("priceOptions") or [])
    category     = _infer_category(product)

    # Location — use advertised location or city from product
    location     = product.get("advertisedLocations") or []
    city  = ""
    state = ""
    country = (product.get("country") or "US").upper()
    if location:
        first_loc = location[0]
        city  = first_loc.get("city") or ""
        state = first_loc.get("state") or ""
    if not city:
        city = (product.get("cityName") or "").strip()

    # Encode all Rezdy booking params in booking_url (internal, never shown to users)
    # RezdyBooker parses this JSON to execute the booking via POST /bookings
    booking_params = json.dumps({
        "_type":           "rezdy",
        "api_key_env":     "REZDY_API_KEY",
        "product_code":    product_code,
        "start_time_local": start_str,      # must match exactly for booking
        "option_label":    _primary_option_label(session),
    })

    raw = {
        "business_id":      product_code,
        "business_name":    product.get("supplierName") or product_name,
        "category":         category,
        "service_name":     product_name,
        "start_time":       start_iso,
        "end_time":         end_iso,
        "duration_minutes": duration_min,
        "price":            price,
        "currency":         product.get("currency") or "USD",
        "location_city":    city,
        "location_state":   state,
        "location_country": country,
        "booking_url":      booking_params,
        "data_source":      "api",
        "confidence":       "high" if seats_available is not None else "medium",
        "spots_open":       seats_available,
        "spots_total":      seats_total,
    }

    slot = normalize(raw, platform="rezdy")
    slot["spots_open"]  = seats_available
    slot["spots_total"] = seats_total
    return slot


def _primary_option_label(session: dict) -> str:
    """Pick the primary price option label (Adult > Standard > General > first)."""
    options = session.get("priceOptions") or []
    preferred = ["adult", "adults", "standard", "general", "person", "1 person"]
    for pref in preferred:
        for opt in options:
            label = (opt.get("label") or "").lower()
            if pref in label:
                return opt.get("label", "Adult")
    if options:
        return options[0].get("label", "Adult")
    return "Adult"


# ── Rezdy API client ─────────────────────────────────────────────────────────

class RezdyClient:
    def __init__(self, api_key: str, base_url: str = REZDY_API_BASE, timeout: int = 30):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self.session  = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> dict:
        p = {"apiKey": self.api_key}
        if params:
            p.update(params)
        resp = self.session.get(f"{self.base_url}/{path}", params=p, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_products(self, limit: int = 100, offset: int = 0) -> list[dict]:
        data = self._get("products", {"limit": limit, "offset": offset})
        return data.get("products") or []

    def get_availability(
        self,
        product_code: str,
        start_time: str,
        end_time: str,
        qty: int = 1,
    ) -> list[dict]:
        data = self._get(
            "availability",
            {
                "productCode": product_code,
                "startTime":   start_time,
                "endTime":     end_time,
                "qty":         qty,
            },
        )
        return data.get("sessions") or []


# ── Main fetch logic ─────────────────────────────────────────────────────────

def fetch_rezdy(hours_ahead: float = 72.0, staging: bool = False, limit: int = 100) -> list[dict]:
    api_key = os.getenv("REZDY_API_KEY", "").strip()
    if not api_key:
        print("  SKIP — REZDY_API_KEY not set in .env")
        print("  Get a free API key: rezdy.com → signup as Reseller → Integrations → Request API Key")
        return []

    base_url = REZDY_STAGING_BASE if staging else REZDY_API_BASE
    env_label = "STAGING" if staging else "PRODUCTION"
    print(f"  [Rezdy {env_label}] Connecting to {base_url} ...")

    client = RezdyClient(api_key, base_url)

    # Date window: now → now + hours_ahead + buffer
    now       = datetime.now(timezone.utc)
    start_str = now.strftime("%Y-%m-%d %H:%M:%S")
    end_str   = (now + timedelta(hours=hours_ahead + 1)).strftime("%Y-%m-%d %H:%M:%S")

    # Fetch all products this agent has access to
    all_products: list[dict] = []
    offset = 0
    while True:
        try:
            batch = client.get_products(limit=limit, offset=offset)
        except requests.HTTPError as exc:
            print(f"  [Rezdy] ERROR fetching products: {exc.response.status_code} {exc.response.text[:200]}")
            break
        except Exception as exc:
            print(f"  [Rezdy] ERROR fetching products: {exc}")
            break

        if not batch:
            break
        all_products.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(REQUEST_DELAY_S)

    print(f"  [Rezdy] {len(all_products)} products accessible")
    if not all_products:
        print("  [Rezdy] No products returned — suppliers may not have granted rate access yet.")
        print("  [Rezdy] This is expected on a new account. Use Rezdy Marketplace to request access.")
        return []

    slots: list[dict] = []

    for product in all_products:
        product_code = product.get("productCode", "")
        if not product_code:
            continue

        try:
            sessions = client.get_availability(
                product_code=product_code,
                start_time=start_str,
                end_time=end_str,
            )
            time.sleep(REQUEST_DELAY_S)
        except requests.HTTPError as exc:
            # 403 = supplier hasn't granted access; skip silently
            if exc.response.status_code == 403:
                continue
            print(f"  [Rezdy] [{product_code}] availability error: {exc.response.status_code}")
            continue
        except Exception as exc:
            print(f"  [Rezdy] [{product_code}] availability error: {exc}")
            continue

        for session in sessions:
            slot = session_to_slot(session, product)
            if slot:
                slots.append(slot)

    print(f"  [Rezdy] {len(slots)} bookable slots within {hours_ahead}h")
    return slots


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch Rezdy slot availability")
    parser.add_argument("--hours-ahead", type=float, default=72.0)
    parser.add_argument("--staging", action="store_true",
                        help="Use Rezdy staging environment (api.rezdy-staging.com)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Products per page (default: 100)")
    args = parser.parse_args()

    print(f"Fetching Rezdy availability (window: {args.hours_ahead}h, "
          f"{'STAGING' if args.staging else 'PRODUCTION'}) ...")

    slots = fetch_rezdy(
        hours_ahead=args.hours_ahead,
        staging=args.staging,
        limit=args.limit,
    )

    slots.sort(key=lambda s: s.get("hours_until_start") or 9999)

    TMP_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(slots, indent=2, default=str),
        encoding="utf-8",
    )

    open_count = sum(1 for s in slots if (s.get("spots_open") or 1) > 0)
    print(f"\nRezdy fetch complete: {len(slots)} slots ({open_count} with open spots)")
    print(f"Output: {OUTPUT_FILE}")
    return len(slots)


if __name__ == "__main__":
    count = main()
    sys.exit(0 if count >= 0 else 1)
