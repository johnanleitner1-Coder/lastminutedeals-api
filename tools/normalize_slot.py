"""
normalize_slot.py — Shared schema library for slot normalization.

Imported by all platform fetch tools. Defines the canonical slot schema,
slot_id hashing, and validation. Never run directly.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional


# ── Canonical category enum ───────────────────────────────────────────────────
VALID_CATEGORIES = {
    "wellness",
    "beauty",
    "hospitality",
    "home_services",
    "professional_services",
    "events",
    "experiences",   # tours, activities, and adventures (OCTO vertical)
}

# ── Canonical platform enum ───────────────────────────────────────────────────
VALID_PLATFORMS = {
    "mindbody",
    "google_reserve",
    "airbnb",
    "booking_com",
    "booksy",
    "thumbtack",
    "angi",
    "eventbrite",
    "meetup",
    "ticketmaster",
    "seatgeek",
    "fareharbor",
    "dice",
    "luma",
    # ── OCTO-compliant platforms ───────────────────────────────────────────────
    "octo",       # generic OCTO — covers Ventrata, Bokun, Peek Pro, Xola, Zaui, etc.
    "ventrata",
    "bokun",
    "rezdy",
    "xola",
    "peek",
    "zaui",
    "checkfront",
    "test",
}

VALID_DATA_SOURCES = {"api", "scrape", "ical"}
VALID_CONFIDENCE   = {"high", "medium", "low"}

# Required fields every normalized slot must have
REQUIRED_FIELDS = [
    "slot_id",
    "platform",
    "business_id",
    "business_name",
    "category",
    "service_name",
    "start_time",
    "location_city",
    "location_state",
    "booking_url",
    "scraped_at",
    "data_source",
    "confidence",
]


def compute_slot_id(platform: str, business_id: str, start_time: str) -> str:
    """
    Deterministic sha256 hash of platform + business_id + start_time.
    Same open slot discovered on consecutive runs produces the same slot_id,
    enabling upsert deduplication in write_to_sheets.py.

    Args:
        platform:    e.g. "mindbody"
        business_id: platform-native identifier
        start_time:  ISO 8601 UTC string e.g. "2026-03-25T14:00:00Z"

    Returns:
        12-character hex prefix of the sha256 hash (collision probability negligible
        at expected data volumes; use full hash if needed)
    """
    raw = f"{platform}::{business_id}::{start_time}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def compute_hours_until(start_time_iso: str) -> Optional[float]:
    """
    Compute hours between now (UTC) and start_time.
    Returns None if start_time cannot be parsed.
    """
    try:
        if start_time_iso.endswith("Z"):
            start_time_iso = start_time_iso[:-1] + "+00:00"
        start_dt = datetime.fromisoformat(start_time_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (start_dt - now).total_seconds() / 3600
        return round(delta, 2)
    except Exception:
        return None


def normalize(raw: dict, platform: str) -> dict:
    """
    Build a canonical slot record from a raw platform response.

    Callers (fetch_*.py scripts) pass their platform-specific dict;
    this function stamps the standard fields that every tool must provide.
    Fields not in raw default to None.

    The calling fetch_* tool is responsible for mapping its platform fields
    to the keys expected here before calling normalize().

    Required keys the caller must provide in `raw`:
        business_id, business_name, category, service_name,
        start_time (ISO 8601 UTC), location_city, location_state,
        booking_url, data_source, confidence

    Optional keys (will default to None if absent):
        end_time, duration_minutes, price, currency, original_price,
        location_country, latitude, longitude, raw_data
    """
    platform = platform.lower().strip()
    now_iso  = datetime.now(timezone.utc).isoformat()

    business_id = str(raw.get("business_id", ""))
    start_time  = raw.get("start_time", "")

    slot = {
        # ── Identity ──────────────────────────────────────────────────────────
        "slot_id":          compute_slot_id(platform, business_id, start_time),
        "platform":         platform,
        "business_id":      business_id,
        "business_name":    raw.get("business_name"),

        # ── Service info ──────────────────────────────────────────────────────
        "category":         raw.get("category"),
        "service_name":     raw.get("service_name"),

        # ── Timing ───────────────────────────────────────────────────────────
        "start_time":       start_time,
        "end_time":         raw.get("end_time"),
        "duration_minutes": raw.get("duration_minutes"),
        "hours_until_start": compute_hours_until(start_time),

        # ── Pricing (source price — our price set by compute_pricing.py) ──────
        "price":            raw.get("price"),
        "currency":         raw.get("currency", "USD"),
        "original_price":   raw.get("original_price"),
        "our_price":        None,   # populated by compute_pricing.py
        "our_markup":       None,   # populated by compute_pricing.py

        # ── Location ─────────────────────────────────────────────────────────
        "location_city":    raw.get("location_city"),
        "location_state":   raw.get("location_state"),
        "location_country": raw.get("location_country", "US"),
        "latitude":         raw.get("latitude"),
        "longitude":        raw.get("longitude"),

        # ── Booking ───────────────────────────────────────────────────────────
        # booking_url is INTERNAL ONLY — never shown to the end user
        "booking_url":      raw.get("booking_url"),

        # ── Metadata ──────────────────────────────────────────────────────────
        "scraped_at":       now_iso,
        "data_source":      raw.get("data_source", "api"),
        "confidence":       raw.get("confidence", "medium"),
        "raw_data":         json.dumps(raw.get("raw_data")) if raw.get("raw_data") else None,
    }

    return slot


def validate_schema(slot: dict) -> tuple[bool, list[str]]:
    """
    Validate a normalized slot record.

    Returns:
        (is_valid: bool, errors: list[str])
    """
    errors = []

    # Required fields present and non-empty
    for field in REQUIRED_FIELDS:
        if not slot.get(field):
            errors.append(f"Missing required field: {field}")

    # Enum validation
    if slot.get("platform") and slot["platform"] not in VALID_PLATFORMS:
        errors.append(f"Invalid platform: {slot['platform']}")

    if slot.get("category") and slot["category"] not in VALID_CATEGORIES:
        errors.append(f"Invalid category: {slot['category']}")

    if slot.get("data_source") and slot["data_source"] not in VALID_DATA_SOURCES:
        errors.append(f"Invalid data_source: {slot['data_source']}")

    if slot.get("confidence") and slot["confidence"] not in VALID_CONFIDENCE:
        errors.append(f"Invalid confidence: {slot['confidence']}")

    # hours_until_start must be computable and ≤ 72
    hours = slot.get("hours_until_start")
    if hours is None:
        errors.append("Could not compute hours_until_start — check start_time format")
    elif hours > 72:
        errors.append(f"Slot is more than 72 hours away ({hours:.1f}h) — should be filtered before validation")
    elif hours < 0:
        errors.append(f"Slot start_time is in the past ({hours:.1f}h)")

    # Price sanity
    if slot.get("price") is not None:
        try:
            float(slot["price"])
        except (TypeError, ValueError):
            errors.append(f"price is not numeric: {slot['price']}")

    return (len(errors) == 0, errors)


def is_within_window(slot: dict, hours_ahead: float = 72.0) -> bool:
    """Return True if the slot starts within `hours_ahead` hours and is not in the past."""
    hours = slot.get("hours_until_start")
    if hours is None:
        return False
    return 0 <= hours <= hours_ahead
