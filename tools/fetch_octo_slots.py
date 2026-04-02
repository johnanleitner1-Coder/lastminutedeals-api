"""
fetch_octo_slots.py — Fetch last-minute availability from OCTO-compliant booking platforms.

OCTO (Open Connectivity for Tourism) is an open standard implemented by 130+ suppliers
including Ventrata, Bokun, Peek Pro, Xola, Zaui, Checkfront, and others. One integration
format, many suppliers.

Reads supplier configs from tools/seeds/octo_suppliers.json. Only suppliers with
`enabled: true` and a configured API key (via .env) are queried.

Usage:
    python tools/fetch_octo_slots.py [--hours-ahead 72] [--test-only]

Output:
    .tmp/octo_slots.json  — normalized slot records

── Getting started ─────────────────────────────────────────────────────────────

Ventrata (fastest path — test sandbox available immediately, no signup):
  1. Get test API key from: https://docs.ventrata.com/getting-started/getting-started
  2. Add to .env:  VENTRATA_API_KEY=<your_key>
  3. Set enabled=true for ventrata_edinexplore in tools/seeds/octo_suppliers.json
  4. Run: python tools/fetch_octo_slots.py --test-only

Bokun ($49/month, self-serve):
  1. Sign up at bokun.io, select Reseller role
  2. Generate API key: Settings > Connections > API Keys
  3. Add to .env:  BOKUN_API_KEY=<your_key>
  4. Set enabled=true for bokun_reseller in tools/seeds/octo_suppliers.json

── OCTO API reference ───────────────────────────────────────────────────────────
  GET  /products                        — list all products for this supplier
  POST /availability                    — get availability for a product/date range
  POST /reservations                    — create a reservation (hold)
  POST /bookings/{uuid}/confirm         — confirm a reservation
  Auth: Authorization: Bearer {api_key}
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
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from normalize_slot import normalize, compute_slot_id

BASE_DIR       = Path(__file__).parent.parent
SEEDS_DIR      = BASE_DIR / "tools" / "seeds"
TMP_DIR        = BASE_DIR / ".tmp"
OUTPUT_FILE    = TMP_DIR / "octo_slots.json"
SUPPLIERS_FILE = SEEDS_DIR / "octo_suppliers.json"

# OCTO availability statuses that mean "bookable"
BOOKABLE_STATUSES = {"AVAILABLE", "FREESALE", "LIMITED"}

# Delay between API calls per supplier (be a good API citizen)
REQUEST_DELAY_S = 0.5


# ── OCTO HTTP client ──────────────────────────────────────────────────────────

class OCTOClient:
    """Thin wrapper around the OCTO REST API for a single supplier."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.timeout  = timeout
        self.session  = requests.Session()
        self.session.headers.update({
            "Authorization":  f"Bearer {api_key}",
            "Content-Type":   "application/json",
            "Accept":         "application/json",
            "Octo-Capabilities": "octo/pricing",   # required by Ventrata; provides pricing data
        })

    def get_products(self) -> list[dict]:
        """Fetch all products (experiences/activities) from this supplier."""
        resp = self.session.get(
            f"{self.base_url}/products",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_availability(
        self,
        product_id: str,
        option_id: str,
        units: list[dict],
        date_start: str,
        date_end: str,
    ) -> list[dict]:
        """
        POST /availability — get all available time slots for a product in a date range.

        Args:
            product_id:  OCTO product identifier
            option_id:   OCTO option identifier (usually "DEFAULT")
            units:       list of {id: unit_type_id, quantity: int}
            date_start:  local date string "YYYY-MM-DD"
            date_end:    local date string "YYYY-MM-DD"

        Returns:
            List of availability objects with id, localDateTimeStart,
            localDateTimeEnd, status, vacancies, capacity, unitPricing
        """
        payload = {
            "productId":      product_id,
            "optionId":       option_id,
            "localDateStart": date_start,
            "localDateEnd":   date_end,
            "units":          units,
        }
        resp = self.session.post(
            f"{self.base_url}/availability",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


# ── Slot normalization ────────────────────────────────────────────────────────

def _parse_octo_datetime(dt_str: str) -> datetime | None:
    """Parse an OCTO localDateTimeStart/End string to a UTC-aware datetime."""
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_price(unit_pricing: list[dict]) -> float | None:
    """Return the retail price for the first adult/standard unit type, in dollars."""
    if not unit_pricing:
        return None
    # Prefer adult, then standard, then first available
    preferred = ["adult", "adults", "standard", "general", "person"]
    ordered = sorted(
        unit_pricing,
        key=lambda u: next(
            (i for i, p in enumerate(preferred) if p in (u.get("unitId", "")).lower()),
            99,
        ),
    )
    for unit in ordered:
        retail = unit.get("retail")
        if retail is not None:
            return round(retail / 100, 2)  # OCTO prices are in cents
    return None


def _primary_unit(product: dict) -> tuple[str, str]:
    """
    Return (option_id, unit_id) for the primary booking unit.
    Falls back to ("DEFAULT", first unit type id).
    """
    options = product.get("options") or []
    option_id = "DEFAULT"
    unit_id   = "adult"

    if options:
        first_option = options[0]
        option_id = first_option.get("id", "DEFAULT")
        units = first_option.get("units") or []
        # Pick first non-free unit type (skip "child_free" etc.)
        for u in units:
            uid = (u.get("id") or u.get("type") or "adult").lower()
            if "free" not in uid:
                unit_id = u.get("id") or uid
                break
        if not units and unit_id == "adult":
            unit_id = "adult"

    return option_id, unit_id


def _infer_category(product: dict, supplier_category: str) -> str:
    """Map OCTO product category tags to our normalized category enum."""
    name_lower = (product.get("internalName") or product.get("title") or "").lower()
    tags = [t.lower() for t in (product.get("tags") or [])]

    wellness_kw  = ["yoga", "pilates", "massage", "spa", "meditation", "fitness", "gym", "wellness"]
    beauty_kw    = ["salon", "haircut", "beauty", "nail", "barber", "facial", "skincare"]
    hospitality_kw = ["hotel", "accommodation", "stay", "lodge"]

    for kw in wellness_kw:
        if kw in name_lower or any(kw in t for t in tags):
            return "wellness"
    for kw in beauty_kw:
        if kw in name_lower or any(kw in t for t in tags):
            return "beauty"
    for kw in hospitality_kw:
        if kw in name_lower or any(kw in t for t in tags):
            return "hospitality"

    return supplier_category or "experiences"


def octo_availability_to_slot(
    avail: dict,
    product: dict,
    option_id: str,
    unit_id: str,
    supplier: dict,
) -> dict | None:
    """
    Convert one OCTO availability record into our normalized slot schema.
    Returns None if the slot is not bookable or is out of the time window.
    """
    status = avail.get("status", "")
    if status not in BOOKABLE_STATUSES:
        return None

    start_dt = _parse_octo_datetime(avail.get("localDateTimeStart", ""))
    end_dt   = _parse_octo_datetime(avail.get("localDateTimeEnd", ""))

    if not start_dt:
        return None

    now = datetime.now(timezone.utc)
    hours_until = (start_dt - now).total_seconds() / 3600

    # Only keep slots in the future and within 72 hours
    if hours_until < 0 or hours_until > 72:
        return None

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if end_dt else None

    duration_min = None
    if start_dt and end_dt:
        duration_min = int((end_dt - start_dt).total_seconds() / 60)

    vacancies = avail.get("vacancies")
    capacity  = avail.get("capacity")
    price     = _extract_price(avail.get("unitPricing") or [])

    product_id    = product.get("id", "")
    product_name  = product.get("internalName") or product.get("title") or product_id
    supplier_name = supplier.get("name", "OCTO Supplier")
    city          = supplier.get("city", "")
    state         = supplier.get("state", "")
    country       = supplier.get("country", "US")
    category      = _infer_category(product, supplier.get("category", "experiences"))

    # Encode all OCTO booking params in booking_url (internal, never shown to users)
    # OCTOBooker will parse this JSON to execute the reservation + confirmation
    booking_params = json.dumps({
        "_type":          "octo",
        "base_url":       supplier["base_url"],
        "api_key_env":    supplier["api_key_env"],
        "product_id":     product_id,
        "option_id":      option_id,
        "availability_id": avail.get("id", ""),
        "unit_id":        unit_id,
        "supplier_id":    supplier.get("supplier_id", ""),
    })

    raw = {
        "business_id":    f"{supplier.get('supplier_id', 'octo')}_{product_id}",
        "business_name":  supplier_name,
        "category":       category,
        "service_name":   product_name,
        "start_time":     start_iso,
        "end_time":       end_iso,
        "duration_minutes": duration_min,
        "price":          price,
        "currency":       "USD",
        "location_city":  city,
        "location_state": state,
        "location_country": country,
        "booking_url":    booking_params,
        "data_source":    "api",
        "confidence":     "high" if vacancies is not None else "medium",
        "spots_open":     vacancies,
        "spots_total":    capacity,
        "teacher":        None,
    }

    slot = normalize(raw, platform="octo")

    # Carry forward availability/spots fields (normalize() doesn't include them)
    slot["spots_open"]  = vacancies
    slot["spots_total"] = capacity

    return slot


# ── Per-supplier fetch ────────────────────────────────────────────────────────

def fetch_supplier(supplier: dict, hours_ahead: float = 72.0) -> list[dict]:
    """Fetch all available slots from one OCTO-compliant supplier."""
    name       = supplier.get("name", "Unknown")
    base_url   = supplier.get("base_url", "")
    api_key_env = supplier.get("api_key_env", "")
    api_key    = os.getenv(api_key_env, "").strip()

    if not api_key:
        print(f"  [{name}] SKIP — {api_key_env} not set in .env")
        return []

    print(f"  [{name}] Connecting to {base_url} ...")

    client = OCTOClient(base_url, api_key)

    # Compute date range: today through today + ceil(hours_ahead/24) days
    now        = datetime.now(timezone.utc)
    date_start = now.strftime("%Y-%m-%d")
    date_end   = (now + timedelta(hours=hours_ahead + 24)).strftime("%Y-%m-%d")

    try:
        products = client.get_products()
        print(f"  [{name}] {len(products)} products")
    except requests.HTTPError as exc:
        print(f"  [{name}] ERROR getting products: {exc.response.status_code} {exc.response.text[:200]}")
        return []
    except Exception as exc:
        print(f"  [{name}] ERROR getting products: {exc}")
        return []

    slots = []

    for product in products:
        product_id = product.get("id", "")
        if not product_id:
            continue

        option_id, unit_id = _primary_unit(product)
        units = [{"id": unit_id, "quantity": 1}]

        try:
            availability = client.get_availability(
                product_id=product_id,
                option_id=option_id,
                units=units,
                date_start=date_start,
                date_end=date_end,
            )
            time.sleep(REQUEST_DELAY_S)
        except requests.HTTPError as exc:
            print(f"  [{name}] [{product_id}] availability error: {exc.response.status_code}")
            continue
        except Exception as exc:
            print(f"  [{name}] [{product_id}] availability error: {exc}")
            continue

        for avail in availability:
            slot = octo_availability_to_slot(avail, product, option_id, unit_id, supplier)
            if slot:
                slots.append(slot)

    print(f"  [{name}] {len(slots)} bookable slots within {hours_ahead}h")
    return slots


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch OCTO slot availability")
    parser.add_argument("--hours-ahead", type=float, default=72.0,
                        help="Only include slots within this many hours (default: 72)")
    parser.add_argument("--test-only", action="store_true",
                        help="Only run suppliers with test_mode=true")
    args = parser.parse_args()

    if not SUPPLIERS_FILE.exists():
        print(f"ERROR: Supplier config not found: {SUPPLIERS_FILE}")
        sys.exit(1)

    suppliers = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    enabled   = [
        s for s in suppliers
        if s.get("enabled", False)
        and (not s.get("api_format"))        # only pure OCTO suppliers (not rezdy/xola/fareharbor)
        and (not args.test_only or s.get("test_mode", False))
    ]

    if not enabled:
        print("No enabled OCTO suppliers found.")
        print(f"Edit {SUPPLIERS_FILE} and set enabled=true for at least one supplier,")
        print("then add the corresponding API key to .env.")
        TMP_DIR.mkdir(exist_ok=True)
        OUTPUT_FILE.write_text("[]", encoding="utf-8")
        sys.exit(0)

    print(f"Fetching OCTO availability from {len(enabled)} supplier(s) "
          f"(window: {args.hours_ahead}h) ...")

    all_slots: list[dict] = []
    for supplier in enabled:
        try:
            slots = fetch_supplier(supplier, hours_ahead=args.hours_ahead)
            all_slots.extend(slots)
        except Exception as exc:
            print(f"  [{supplier.get('name')}] FATAL: {exc}")

    # Sort soonest first
    all_slots.sort(key=lambda s: s.get("hours_until_start") or 9999)

    TMP_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(all_slots, indent=2, default=str),
        encoding="utf-8",
    )

    open_count = sum(1 for s in all_slots if (s.get("spots_open") or 1) > 0)
    print(f"\nOCTO fetch complete: {len(all_slots)} slots ({open_count} with open spots)")
    print(f"Output: {OUTPUT_FILE}")
    return len(all_slots)


if __name__ == "__main__":
    count = main()
    sys.exit(0 if count >= 0 else 1)
