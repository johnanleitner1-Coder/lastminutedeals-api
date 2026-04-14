"""
fetch_liquidspace_slots.py — Fetch last-minute workspace availability via LiquidSpace Marketplace API.

LiquidSpace is a marketplace for on-demand office space, meeting rooms, and coworking desks.
The Marketplace API provides real-time hourly availability and supports reservation creation.
Auth is OAuth 2.0 client_credentials flow via the Identity API.

Usage:
    python tools/fetch_liquidspace_slots.py [--hours-ahead 72] [--city "New York"] [--test]

Output:
    .tmp/liquidspace_slots.json  — normalized slot records

── Getting started ──────────────────────────────────────────────────────────────

1. Sign in at developer.liquidspace.com (use your LiquidSpace account)
2. Create an application → get client_id and client_secret
3. Note the API base URL (shown in the portal under Marketplace API)
4. Add to .env:
      LIQUIDSPACE_CLIENT_ID=<your_client_id>
      LIQUIDSPACE_CLIENT_SECRET=<your_client_secret>
      LIQUIDSPACE_API_BASE=<base_url_from_portal>  # e.g. https://api.liquidspace.com
5. Run: python tools/fetch_liquidspace_slots.py --test

── LiquidSpace Marketplace API ───────────────────────────────────────────────────
  POST /identity/connect/token                      — get OAuth 2.0 bearer token
  POST /marketplace/v1/search                       — search available workspaces
  GET  /marketplace/v1/venues/{venueId}/availability — hourly availability for a venue
  GET  /marketplace/v1/workspaces/{workspaceId}/availability — workspace availability
  GET  /marketplace/v1/venues/{venueId}             — venue details
  GET  /marketplace/v1/workspaces/{workspaceId}     — workspace details
  POST /marketplace/v1/reservations                 — create reservation
  DELETE /marketplace/v1/reservations/{id}          — cancel reservation
  Auth: Authorization: Bearer {access_token}
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
OUTPUT_FILE = TMP_DIR / "liquidspace_slots.json"
TOKEN_CACHE = TMP_DIR / "_liquidspace_token.json"

REQUEST_DELAY_S = 0.5


# ── OAuth token management ─────────────────────────────────────────────────────

def _get_token(base_url: str, client_id: str, client_secret: str) -> str | None:
    """
    Get an OAuth 2.0 access token using client_credentials grant.
    Caches the token locally until it expires (with 60s buffer).
    """
    # Check cache
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(cached.get("expires_at", "2000-01-01T00:00:00+00:00"))
            if datetime.now(timezone.utc) < expires_at:
                return cached["access_token"]
        except Exception:
            pass

    # Fetch new token
    token_url = f"{base_url.rstrip('/')}/identity/connect/token"
    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "lsapi.marketplace",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [LiquidSpace] Token request failed: {e}")
        return None

    token = data.get("access_token")
    expires_in = data.get("expires_in", 3600)

    if token:
        # Cache with expiry buffer
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)).isoformat()
        try:
            TMP_DIR.mkdir(exist_ok=True)
            TOKEN_CACHE.write_text(
                json.dumps({"access_token": token, "expires_at": expires_at}),
                encoding="utf-8",
            )
        except Exception:
            pass

    return token


# ── API client ─────────────────────────────────────────────────────────────────

class LiquidSpaceClient:
    def __init__(self, base_url: str, access_token: str, subscription_key: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self.session  = requests.Session()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        if subscription_key:
            headers["Ocp-Apim-Subscription-Key"] = subscription_key
        self.session.headers.update(headers)

    def search_workspaces(
        self,
        date: str,          # YYYY-MM-DD
        start_hour: int,    # 0-23
        end_hour: int,      # 0-23
        city: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
        radius_miles: int = 25,
        workspace_types: list[str] | None = None,
    ) -> list[dict]:
        """
        Execute a full workspace search for available slots on a given date/time.
        Returns list of workspace availability records.
        """
        payload = {
            "date":         date,
            "startHour":    start_hour,
            "endHour":      end_hour,
            "radiusMiles":  radius_miles,
        }
        if city:
            payload["city"] = city
        if latitude is not None and longitude is not None:
            payload["latitude"]  = latitude
            payload["longitude"] = longitude
        if workspace_types:
            payload["workspaceTypes"] = workspace_types

        try:
            resp = self.session.post(
                f"{self.base_url}/marketplace/v1/search",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # Response may be a list or {"results": [...]}
            if isinstance(data, list):
                return data
            return data.get("results") or data.get("workspaces") or []
        except requests.HTTPError as exc:
            print(f"  [LiquidSpace] Search error: {exc.response.status_code} {exc.response.text[:300]}")
            return []
        except Exception as exc:
            print(f"  [LiquidSpace] Search error: {exc}")
            return []

    def get_venue_availability(
        self,
        venue_id: str,
        date: str,          # YYYY-MM-DD
    ) -> dict:
        """Get hourly availability for all workspaces at a venue on a given date."""
        try:
            resp = self.session.get(
                f"{self.base_url}/marketplace/v1/venues/{venue_id}/availability",
                params={"date": date},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def get_workspace_availability(
        self,
        workspace_id: str,
        date: str,
    ) -> dict:
        """Get hourly availability for a specific workspace on a given date."""
        try:
            resp = self.session.get(
                f"{self.base_url}/marketplace/v1/workspaces/{workspace_id}/availability",
                params={"date": date},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def get_workspace_details(self, workspace_id: str) -> dict:
        """Get full details for a workspace."""
        try:
            resp = self.session.get(
                f"{self.base_url}/marketplace/v1/workspaces/{workspace_id}",
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def search_venues_by_prefix(self, term: str) -> list[dict]:
        """Discover venues matching a search term (useful for seeding city lists)."""
        try:
            resp = self.session.get(
                f"{self.base_url}/marketplace/v1/venues/search",
                params={"term": term},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("venues") or []
        except Exception:
            return []


# ── Slot normalization ─────────────────────────────────────────────────────────

def _infer_workspace_category(workspace: dict) -> str:
    """Map LiquidSpace workspace types to our category enum."""
    wtype = (workspace.get("workspaceType") or workspace.get("type") or "").lower()
    name  = (workspace.get("name") or "").lower()
    text  = f"{wtype} {name}"

    if any(k in text for k in ["meeting", "conference", "board"]):
        return "professional_services"
    if any(k in text for k in ["private office", "dedicated desk", "exec"]):
        return "professional_services"
    if any(k in text for k in ["cowork", "hot desk", "open", "lounge"]):
        return "professional_services"
    return "professional_services"   # all workspace = professional_services by default


def workspace_slot_to_slot(
    workspace: dict,
    date: str,      # YYYY-MM-DD
    start_hour: int,
    end_hour: int,
    hours_until: float,
) -> dict | None:
    """Convert a LiquidSpace workspace + time window into a normalized slot."""
    workspace_id  = str(workspace.get("id") or workspace.get("workspaceId") or "")
    venue         = workspace.get("venue") or {}
    venue_id      = str(venue.get("id") or venue.get("venueId") or workspace.get("venueId") or "")
    business_id   = workspace_id or venue_id
    if not business_id:
        return None

    # Location
    address   = workspace.get("address") or venue.get("address") or {}
    city      = (address.get("city") or venue.get("city") or workspace.get("city") or "").strip()
    state     = (address.get("state") or venue.get("state") or workspace.get("state") or "").strip()
    country   = (address.get("country") or "US").upper()
    latitude  = workspace.get("latitude") or venue.get("latitude")
    longitude = workspace.get("longitude") or venue.get("longitude")

    # Service info
    venue_name  = venue.get("name") or workspace.get("venueName") or ""
    space_name  = workspace.get("name") or workspace.get("workspaceName") or ""
    service_name = f"{space_name}" if space_name else venue_name
    business_name = venue_name or space_name

    # Pricing — LiquidSpace typically returns hourly rate
    hourly_rate = workspace.get("hourlyRate") or workspace.get("pricePerHour") or workspace.get("price")
    duration_hours = max(1, end_hour - start_hour)
    price = None
    if hourly_rate is not None:
        try:
            price = round(float(hourly_rate) * duration_hours, 2)
        except (ValueError, TypeError):
            pass

    # Build start/end ISO timestamps
    try:
        start_dt = datetime.strptime(f"{date}T{start_hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        end_dt   = datetime.strptime(f"{date}T{end_hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Encode booking params (parsed by LiquidSpaceBooker in complete_booking.py)
    booking_params = json.dumps({
        "_type":        "liquidspace",
        "workspace_id": workspace_id,
        "venue_id":     venue_id,
        "date":         date,
        "start_hour":   start_hour,
        "end_hour":     end_hour,
        "hourly_rate":  hourly_rate,
    })

    raw = {
        "business_id":      business_id,
        "business_name":    business_name,
        "category":         _infer_workspace_category(workspace),
        "service_name":     service_name or "Workspace",
        "start_time":       start_iso,
        "end_time":         end_iso,
        "duration_minutes": duration_hours * 60,
        "price":            price,
        "currency":         "USD",
        "location_city":    city,
        "location_state":   state,
        "location_country": country,
        "latitude":         float(latitude) if latitude else None,
        "longitude":        float(longitude) if longitude else None,
        "booking_url":      booking_params,
        "data_source":      "api",
        "confidence":       "high",
    }

    slot = normalize(raw, platform="liquidspace")
    slot["hourly_rate"]  = hourly_rate
    slot["workspace_id"] = workspace_id
    slot["venue_id"]     = venue_id
    return slot


# ── Main fetch logic ──────────────────────────────────────────────────────────

# Default cities to search if no specific city given
DEFAULT_CITIES = [
    "New York",
    "Los Angeles",
    "Chicago",
    "San Francisco",
    "Seattle",
    "Austin",
    "Boston",
    "Miami",
    "Denver",
    "Atlanta",
]

# Search window: break the 72h ahead into hourly chunks grouped by date
def _build_time_windows(hours_ahead: float) -> list[tuple[str, int, int]]:
    """
    Build a list of (date_str, start_hour, end_hour) search windows.
    LiquidSpace searches by date + hour range within that date.
    We generate 2-hour windows to catch short last-minute bookings.
    """
    now = datetime.now(timezone.utc)
    windows: list[tuple[str, int, int]] = []
    seen_dates: set[str] = set()

    dt = now
    while (dt - now).total_seconds() / 3600 < hours_ahead:
        date_str = dt.strftime("%Y-%m-%d")
        hour     = dt.hour
        # Cover the rest of this day in one window
        if date_str not in seen_dates:
            seen_dates.add(date_str)
            end_of_day = 22  # offices close ~10pm
            if hour < end_of_day:
                windows.append((date_str, hour, end_of_day))
        dt += timedelta(hours=24)

    return windows


def fetch_liquidspace(
    hours_ahead: float = 72.0,
    cities: list[str] | None = None,
    test: bool = False,
) -> list[dict]:
    client_id        = os.getenv("LIQUIDSPACE_CLIENT_ID", "").strip()
    client_secret    = os.getenv("LIQUIDSPACE_CLIENT_SECRET", "").strip()
    subscription_key = os.getenv("LIQUIDSPACE_SUBSCRIPTION_KEY", "").strip()
    base_url         = os.getenv("LIQUIDSPACE_API_BASE", "https://api.liquidspace.com").strip()

    if not client_id or not client_secret:
        print("  SKIP — LIQUIDSPACE_CLIENT_ID / LIQUIDSPACE_CLIENT_SECRET not set in .env")
        print("  Contact support@liquidspace.com to get credentials")
        return []

    print(f"  [LiquidSpace] Connecting to {base_url} ...")

    # Get OAuth token
    token = _get_token(base_url, client_id, client_secret)
    if not token:
        print("  [LiquidSpace] Could not obtain access token — check credentials")
        return []

    client = LiquidSpaceClient(base_url, token, subscription_key)
    target_cities = cities or DEFAULT_CITIES

    if test:
        # In test mode just run one city + one window to verify connectivity
        target_cities = [target_cities[0]]
        print(f"  [LiquidSpace] TEST mode — searching {target_cities[0]} only")

    time_windows = _build_time_windows(hours_ahead)
    if not time_windows:
        print("  [LiquidSpace] No time windows to search")
        return []

    seen_ids: set[str] = set()
    slots: list[dict]  = []

    for city in target_cities:
        print(f"  [LiquidSpace] Searching: {city} ({len(time_windows)} date windows)")
        for date_str, start_hour, end_hour in time_windows:
            results = client.search_workspaces(
                date=date_str,
                start_hour=start_hour,
                end_hour=end_hour,
                city=city,
            )
            time.sleep(REQUEST_DELAY_S)

            now = datetime.now(timezone.utc)
            for ws in results:
                workspace_id = str(ws.get("id") or ws.get("workspaceId") or "")
                dedup_key    = f"{workspace_id}::{date_str}::{start_hour}"
                if dedup_key in seen_ids:
                    continue
                seen_ids.add(dedup_key)

                # Compute hours until start
                try:
                    start_dt = datetime.strptime(
                        f"{date_str}T{start_hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    hours_until = (start_dt - now).total_seconds() / 3600
                except Exception:
                    continue

                if hours_until < 0 or hours_until > hours_ahead:
                    continue

                slot = workspace_slot_to_slot(ws, date_str, start_hour, end_hour, hours_until)
                if slot:
                    slots.append(slot)

    print(f"  [LiquidSpace] {len(slots)} available workspace slots within {hours_ahead}h")
    return slots


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch LiquidSpace workspace availability")
    parser.add_argument("--hours-ahead", type=float, default=72.0,
                        help="Hours ahead to search (default: 72)")
    parser.add_argument("--city",        type=str, default="",
                        help="City to search (default: all major US cities)")
    parser.add_argument("--test",        action="store_true",
                        help="Test mode: single city, single window")
    args = parser.parse_args()

    cities = [args.city] if args.city else None

    print(f"Fetching LiquidSpace workspace availability (window: {args.hours_ahead}h) ...")
    slots = fetch_liquidspace(
        hours_ahead=args.hours_ahead,
        cities=cities,
        test=args.test,
    )

    slots.sort(key=lambda s: s.get("hours_until_start") or 9999)

    TMP_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(slots, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"\nLiquidSpace fetch complete: {len(slots)} workspace slots")
    print(f"Output: {OUTPUT_FILE}")
    return len(slots)


if __name__ == "__main__":
    count = main()
    sys.exit(0 if count >= 0 else 1)
