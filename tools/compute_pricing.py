"""
compute_pricing.py — Dynamic pricing engine.

Reads aggregated_slots.json + PricingLog history from Google Sheets,
computes our_price and our_markup for each slot, writes the result
back to aggregated_slots.json.

Pricing model is determined per supplier from tools/supplier_contracts.json:

  commission  — Customer pays published retail price (our_price = price, markup = 0).
                LMD earns a commission from the supplier after booking completes.
                All current Bokun/OCTO suppliers use this model.

  net_rate    — LMD buys inventory at a negotiated net rate and resells with a
                dynamic markup. Markup signals:
                  1. hours_until_start  — urgency (closer = higher multiplier)
                  2. original price     — higher-value = higher markup ceiling
                  3. category           — some categories sustain higher markups
                  4. supply             — fewer competing slots = higher markup
                  5. historical fill rate — low fill → lower price, high fill → higher
                  6. A/B test group     — tracks which markup level converts best

Usage:
    python tools/compute_pricing.py [--data-file .tmp/aggregated_slots.json]

Output:
    Modifies the input file in-place, adding our_price, our_markup, pricing_model,
    commission_pct, and expected_commission to each slot.
    Also writes pricing decisions to .tmp/pricing_decisions.json for the run log.
"""

import argparse
import json
import os
import random
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

DATA_FILE       = Path(".tmp/aggregated_slots.json")
DECISIONS_FILE  = Path(".tmp/pricing_decisions.json")
CONTRACTS_FILE  = Path(__file__).parent / "supplier_contracts.json"

# Stripe fee constants — used for analytics/margin estimates only, not pricing
STRIPE_PCT  = 0.029   # 2.9%
STRIPE_FLAT = 0.30    # $0.30 per transaction

# ── Pricing model constants ────────────────────────────────────────────────────

# Base markup percentage applied to original price
BASE_MARKUP_PCT = 0.10  # 10%

# Urgency multipliers by time bucket
URGENCY_BRACKETS = [
    (0,  6,  2.5),   # 0–6h:   2.5× (last-chance premium)
    (6,  12, 2.0),   # 6–12h:  2.0×
    (12, 24, 1.5),   # 12–24h: 1.5×
    (24, 48, 1.2),   # 24–48h: 1.2×
    (48, 72, 1.0),   # 48–72h: 1.0× (base)
]

# Category markup ceiling adjustments
# (multiplied on top of the base markup)
CATEGORY_MULTIPLIERS = {
    "wellness":            1.0,
    "beauty":              1.0,
    "hospitality":         1.2,   # hotels/rentals can support higher absolute markup
    "home_services":       0.85,  # more price-sensitive market
    "professional_services": 1.1,
    "experiences":         1.15,  # tours/activities — high perceived value, urgency converts well
    "events":              1.0,
}

# Absolute floor/cap on our markup amount (in $)
MARKUP_FLOOR_USD = 4.00
MARKUP_CAP_PCT   = 0.25    # never charge more than 25% above original price

# A/B test groups — track which markup level converts best per category
# Group A = base pricing, Group B = +20% higher, Group C = -15% lower
AB_GROUPS = ["A", "B", "C"]
AB_WEIGHTS = [0.5, 0.25, 0.25]   # 50% control, 25% higher, 25% lower
AB_ADJUSTMENTS = {"A": 1.0, "B": 1.2, "C": 0.85}


def load_supplier_contracts() -> dict:
    """Load supplier pricing model config from tools/supplier_contracts.json."""
    if not CONTRACTS_FILE.exists():
        print(f"  WARN: {CONTRACTS_FILE} not found — all slots will use net_rate pricing")
        return {}
    try:
        return json.loads(CONTRACTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  WARN: Could not load supplier contracts: {e}")
        return {}


def _resolve_pricing_model(slot: dict, contracts: dict) -> tuple[str, float]:
    """
    Return (pricing_model, commission_pct) for this slot.

    Resolution order (most specific → least specific):
    1. suppliers dict — exact match on business_name
    2. platform_defaults — match on slot's platform field
    3. Fallback — net_rate, 0% commission
    """
    business_name = slot.get("business_name", "")
    platform      = slot.get("platform", "")

    supplier_entry = contracts.get("suppliers", {}).get(business_name)
    if supplier_entry:
        return (
            supplier_entry.get("pricing_model", "net_rate"),
            float(supplier_entry.get("commission_pct", 0.0)),
        )

    platform_entry = contracts.get("platform_defaults", {}).get(platform)
    if platform_entry:
        return (
            platform_entry.get("pricing_model", "net_rate"),
            float(platform_entry.get("commission_pct", 0.0)),
        )

    return "net_rate", 0.0


def get_urgency_multiplier(hours: float) -> float:
    for low, high, mult in URGENCY_BRACKETS:
        if low <= hours < high:
            return mult
    return 1.0  # fallback


def compute_supply_multiplier(slot: dict, all_slots: list[dict]) -> float:
    """
    Count how many competing slots share the same city + category within ±12h.
    Fewer competing slots = higher multiplier (up to 1.3×).
    """
    city     = (slot.get("location_city") or "").lower()
    category = slot.get("category", "")
    h        = slot.get("hours_until_start", 36)

    competitors = sum(
        1 for s in all_slots
        if s.get("slot_id") != slot.get("slot_id")
        and (s.get("location_city") or "").lower() == city
        and s.get("category") == category
        and abs((s.get("hours_until_start") or 36) - h) <= 12
    )

    if competitors == 0:
        return 1.30   # no competition — charge more
    if competitors <= 2:
        return 1.15
    if competitors <= 5:
        return 1.00
    return 0.90       # lots of supply — be competitive


def compute_fill_rate_multiplier(slot: dict, pricing_history: list[dict]) -> float:
    """
    Look at recent pricing history for similar slots (same category + city).
    If fill rate for this category/city is low (<30%), reduce markup slightly.
    If fill rate is high (>70%), raise markup slightly.
    Returns a multiplier in [0.80, 1.20].
    """
    if not pricing_history:
        return 1.0

    category = slot.get("category", "")
    city     = (slot.get("location_city") or "").lower()

    relevant = [
        h for h in pricing_history
        if h.get("category") == category
        and (h.get("location_city") or "").lower() == city
        and h.get("converted") is not None
    ]

    if len(relevant) < 5:
        return 1.0   # not enough data yet

    fill_rate = sum(1 for h in relevant if h.get("converted") == 1) / len(relevant)

    if fill_rate < 0.20:
        return 0.80
    if fill_rate < 0.40:
        return 0.90
    if fill_rate > 0.70:
        return 1.15
    if fill_rate > 0.85:
        return 1.20
    return 1.0


def load_pricing_history() -> list[dict]:
    """
    Load recent PricingLog from Google Sheets if available.
    Falls back to empty list if Sheets is not configured.
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not Path("token.json").exists():
            return []

        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file("token.json")
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                Path("token.json").write_text(creds.to_json())
            except Exception:
                return []
        service = build("sheets", "v4", credentials=creds)

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="PricingLog!A:M",
        ).execute()

        rows = result.get("values", [])
        if len(rows) < 2:
            return []

        headers = rows[0]
        return [dict(zip(headers, row)) for row in rows[1:]]

    except Exception as e:
        print(f"  WARN: Could not load pricing history from Sheets: {e}")
        return []


def price_slot(
    slot: dict,
    all_slots: list[dict],
    pricing_history: list[dict],
    contracts: dict | None = None,
) -> dict:
    """
    Compute our_price and our_markup for a single slot.

    For commission suppliers: our_price = price (pass-through), markup = 0.
    For net_rate suppliers: applies urgency × category × supply × fill rate markup.

    Returns the slot dict with pricing fields added in-place.
    """
    original_price = slot.get("price")

    # If we don't know the original price, we can't price the slot
    if original_price is None:
        slot["our_price"]            = None
        slot["our_markup"]           = None
        slot["markup_pct"]           = None
        slot["test_group"]           = None
        slot["pricing_model"]        = None
        slot["commission_pct"]       = None
        slot["expected_commission"]  = None
        return slot

    try:
        original_price = float(original_price)
    except (TypeError, ValueError):
        slot["our_price"]            = None
        slot["our_markup"]           = None
        slot["markup_pct"]           = None
        slot["test_group"]           = None
        slot["pricing_model"]        = None
        slot["commission_pct"]       = None
        slot["expected_commission"]  = None
        return slot

    # Resolve pricing model for this supplier
    pricing_model, commission_pct = _resolve_pricing_model(slot, contracts or {})
    slot["pricing_model"]  = pricing_model
    slot["commission_pct"] = commission_pct

    # ── Commission model: pass-through pricing ─────────────────────────────
    if pricing_model == "commission":
        # Customer pays the published retail price exactly.
        # LMD earns a commission from the supplier after the booking completes.
        # Never mark up — that would make us more expensive than booking direct.
        our_price = original_price if original_price > 0 else 0.0
        stripe_fee = round(our_price * STRIPE_PCT + STRIPE_FLAT, 2) if our_price > 0 else 0.0
        gross_commission = round(our_price * commission_pct, 2)
        net_commission   = round(gross_commission - stripe_fee, 2)

        slot["our_price"]           = our_price
        slot["our_markup"]          = 0.0
        slot["markup_pct"]          = 0.0
        slot["test_group"]          = None
        slot["expected_commission"] = gross_commission
        slot["expected_net"]        = net_commission   # after Stripe fees
        return slot

    # ── Net-rate model: dynamic markup ────────────────────────────────────
    slot["expected_commission"] = None
    slot["expected_net"]        = None

    # Free events — no markup possible
    if original_price <= 0:
        slot["our_price"]  = 0.0
        slot["our_markup"] = 0.0
        slot["markup_pct"] = 0.0
        slot["test_group"] = None
        return slot

    # Very cheap events: Stripe's $0.30 flat fee makes markup unprofitable.
    # Below $8 the 25% cap overrides the $4 floor, leaving <$1 net.
    if original_price < 8.0:
        slot["our_price"]  = original_price
        slot["our_markup"] = 0.0
        slot["markup_pct"] = 0.0
        slot["test_group"] = None
        return slot

    hours    = slot.get("hours_until_start") or 36
    category = slot.get("category", "wellness")

    # ── Compute multipliers ───────────────────────────────────────────────
    urgency_mult  = get_urgency_multiplier(hours)
    category_mult = CATEGORY_MULTIPLIERS.get(category, 1.0)
    supply_mult   = compute_supply_multiplier(slot, all_slots)
    fill_mult     = compute_fill_rate_multiplier(slot, pricing_history)

    # ── A/B test group assignment (seeded by slot_id for consistency) ─────
    rng        = random.Random(slot.get("slot_id", ""))
    test_group = rng.choices(AB_GROUPS, weights=AB_WEIGHTS)[0]
    ab_mult    = AB_ADJUSTMENTS[test_group]

    # ── Final markup calculation ──────────────────────────────────────────
    combined_mult  = urgency_mult * category_mult * supply_mult * fill_mult * ab_mult
    raw_markup_pct = BASE_MARKUP_PCT * combined_mult
    raw_markup_usd = original_price * raw_markup_pct

    markup_usd = max(MARKUP_FLOOR_USD, raw_markup_usd)
    max_markup = original_price * MARKUP_CAP_PCT
    markup_usd = min(max_markup, markup_usd)

    our_price  = round(original_price + markup_usd, 2)
    markup_pct = round(markup_usd / original_price, 4)

    slot["our_price"]   = our_price
    slot["our_markup"]  = round(markup_usd, 2)
    slot["markup_pct"]  = markup_pct
    slot["test_group"]  = test_group

    return slot


def main():
    parser = argparse.ArgumentParser(description="Compute dynamic pricing for aggregated slots")
    parser.add_argument("--data-file", default=str(DATA_FILE))
    args = parser.parse_args()

    data_path = Path(args.data_file)
    if not data_path.exists():
        print(f"ERROR: {data_path} not found. Run aggregate_slots.py first.")
        sys.exit(1)

    slots = json.loads(data_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(slots)} slots for pricing")

    print("Loading supplier contracts...")
    contracts = load_supplier_contracts()
    n_suppliers = len(contracts.get("suppliers", {}))
    n_defaults  = len(contracts.get("platform_defaults", {}))
    print(f"  {n_suppliers} supplier entries, {n_defaults} platform defaults loaded")

    print("Loading pricing history from Sheets...")
    pricing_history = load_pricing_history()
    print(f"  {len(pricing_history)} historical records loaded")

    decisions  = []
    priced     = 0
    unpriced   = 0
    commission_count = 0
    net_rate_count   = 0

    for i, slot in enumerate(slots):
        slots[i] = price_slot(slot, slots, pricing_history, contracts)
        if slots[i].get("our_price") is not None:
            priced += 1
            model = slots[i].get("pricing_model", "unknown")
            if model == "commission":
                commission_count += 1
            else:
                net_rate_count += 1
            decisions.append({
                "slot_id":              slot.get("slot_id"),
                "platform":             slot.get("platform"),
                "business_name":        slot.get("business_name"),
                "category":             slot.get("category"),
                "location_city":        slot.get("location_city"),
                "hours_until":          slot.get("hours_until_start"),
                "original_price":       slot.get("price"),
                "our_price":            slots[i]["our_price"],
                "our_markup":           slots[i]["our_markup"],
                "markup_pct":           slots[i]["markup_pct"],
                "test_group":           slots[i]["test_group"],
                "pricing_model":        slots[i].get("pricing_model"),
                "commission_pct":       slots[i].get("commission_pct"),
                "expected_commission":  slots[i].get("expected_commission"),
                "expected_net":         slots[i].get("expected_net"),
            })
        else:
            unpriced += 1

    # Write updated slots back in-place
    data_path.write_text(json.dumps(slots, indent=2, default=str), encoding="utf-8")

    # Write decisions log
    DECISIONS_FILE.write_text(json.dumps(decisions, indent=2, default=str), encoding="utf-8")

    print(f"\nPricing complete: {priced} priced ({commission_count} commission, {net_rate_count} net_rate), "
          f"{unpriced} without original price")
    print(f"Output → {data_path} (updated in-place)")
    print(f"Decisions log → {DECISIONS_FILE}")

    # Show samples
    commission_samples = [d for d in decisions if d.get("pricing_model") == "commission"]
    if commission_samples:
        s = commission_samples[0]
        print(f"\nCommission sample: {s.get('business_name', '?')} — {s.get('location_city', '?')}")
        print(f"  Retail: ${s['original_price']}  →  Our price: ${s['our_price']}  "
              f"(commission: {(s['commission_pct'] or 0)*100:.0f}%, "
              f"expected gross: ${s['expected_commission']}, net: ${s['expected_net']})")

    net_rate_samples = [d for d in decisions if d.get("pricing_model") == "net_rate"]
    if net_rate_samples:
        s = net_rate_samples[0]
        print(f"\nNet-rate sample: {s.get('business_name', '?')} — {s.get('location_city', '?')}")
        print(f"  Original: ${s['original_price']}  →  Our price: ${s['our_price']}  "
              f"(+${s['our_markup']}, {(s['markup_pct'] or 0)*100:.1f}%, group {s['test_group']})")


if __name__ == "__main__":
    main()
