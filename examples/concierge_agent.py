"""
concierge_agent.py — Example: AI concierge that handles multi-category booking

This is the production-pattern example. Shows how a travel or lifestyle
concierge AI would use LastMinuteDeals as its backend for ALL last-minute
service bookings — not just one category.

Pattern:
  1. User says what they want in natural language
  2. Concierge maps to category/city/budget
  3. Checks market intelligence to confirm availability
  4. Creates a delegated intent (fire-and-forget)
  5. Notifies user via their preferred channel when booked

This pattern makes LastMinuteDeals the invisible infrastructure layer —
the concierge app never needs its own booking logic.

Run:
    python examples/concierge_agent.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from lmd_sdk import LastMinuteDeals, LastMinuteDealsError


# ── Simple intent parser (real implementation would use an LLM) ───────────────

CATEGORY_KEYWORDS = {
    "wellness":      ["massage", "yoga", "meditation", "spa", "sauna", "facial", "wellness", "fitness", "gym", "pilates"],
    "beauty":        ["haircut", "hair", "nails", "manicure", "pedicure", "waxing", "blowout", "beauty", "salon"],
    "entertainment": ["concert", "show", "event", "game", "performance", "comedy", "theater", "music", "ticket"],
    "hospitality":   ["hotel", "room", "stay", "accommodation", "airbnb", "suite", "lodge", "hostel"],
    "home_services": ["cleaning", "plumber", "electrician", "handyman", "repair", "installation"],
}

def parse_intent(text: str) -> dict:
    """
    Minimal intent parser. In production, replace with LLM call.
    Returns {"category": "...", "city": "...", "budget": float, "urgency": str}
    """
    text_lower = text.lower()

    # Category detection
    category = ""
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            category = cat
            break

    # City detection (naive — real impl would use NER)
    city = ""
    KNOWN_CITIES = ["new york", "nyc", "chicago", "los angeles", "la", "miami",
                    "seattle", "boston", "denver", "austin", "atlanta", "dallas",
                    "houston", "phoenix", "san francisco", "sf", "portland", "detroit"]
    for c in KNOWN_CITIES:
        if c in text_lower:
            city = c.replace("nyc", "New York").replace("la", "Los Angeles").replace("sf", "San Francisco").title()
            break

    # Budget detection
    import re
    budget = None
    match = re.search(r'\$(\d+)', text)
    if match:
        budget = float(match.group(1))
    elif "cheap" in text_lower or "affordable" in text_lower:
        budget = 60.0
    elif "luxury" in text_lower or "premium" in text_lower:
        budget = 300.0

    # Urgency
    urgency = "soon"
    if any(w in text_lower for w in ["tonight", "today", "now", "asap", "immediately", "urgent"]):
        urgency = "immediate"
    elif any(w in text_lower for w in ["tomorrow", "next day"]):
        urgency = "tomorrow"

    return {"category": category, "city": city, "budget": budget, "urgency": urgency}


def hours_from_urgency(urgency: str) -> int:
    return {"immediate": 6, "soon": 24, "tomorrow": 48}.get(urgency, 24)


# ── Concierge ─────────────────────────────────────────────────────────────────

class ConciergeAgent:

    def __init__(self, api_key: str, wallet_id: str, default_city: str = "New York"):
        self.lmd          = LastMinuteDeals(api_key=api_key)
        self.wallet_id    = wallet_id
        self.default_city = default_city

    def handle_request(self, user_request: str, customer: dict, callback_url: str = "") -> dict:
        """
        Process a natural-language booking request.
        Returns the intent session (async) or result (sync).
        """
        print(f"\n[Concierge] Request: \"{user_request}\"")

        # Parse intent
        parsed = parse_intent(user_request)
        category = parsed["category"] or "wellness"  # default to wellness
        city     = parsed["city"] or self.default_city
        budget   = parsed["budget"]
        urgency  = parsed["urgency"]
        hours    = hours_from_urgency(urgency)

        print(f"[Concierge] Parsed: category={category}, city={city}, budget={budget}, urgency={urgency}")

        # Check market intelligence before committing
        insights = self.lmd.insights(category=category, city=city)
        live     = (insights.get("live_inventory") or {}).get(category, {})
        demand   = insights.get("active_demand_signals", 0)

        slot_count = live.get("slot_count", 0)
        next_hours = live.get("next_slot_hours")

        if slot_count == 0:
            print(f"[Concierge] No live {category} slots in {city} right now. Creating a monitoring intent...")
            # Fall back to longer window and create intent
            hours = max(hours, 48)

        if demand > 3:
            print(f"[Concierge] High demand signal: {demand} other agents competing for {category} in {city}.")
            print(f"[Concierge] Upgrading to guaranteed execution to win the race...")

        if next_hours and next_hours < 2 and urgency == "immediate":
            print(f"[Concierge] Slot starts in {next_hours:.1f}h — booking immediately...")

        # Create delegated intent — system does the rest
        intent = self.lmd.intent(
            goal="find_and_book",
            category=category,
            city=city,
            budget=budget,
            hours_ahead=hours,
            customer=customer,
            wallet_id=self.wallet_id,
            autonomy="full",
            callback_url=callback_url,
            ttl_hours=max(hours, 12),
        )

        print(f"[Concierge] Intent created: {intent['intent_id']}")
        print(f"[Concierge] System will auto-book when a match appears. Expires: {intent.get('expires_at', '')[:19]}")

        return {
            "intent_id":   intent["intent_id"],
            "category":    category,
            "city":        city,
            "budget":      budget,
            "slot_count":  slot_count,
            "next_hours":  next_hours,
            "demand":      demand,
            "message":     f"On it! I'm monitoring {category} availability in {city} and will book as soon as a good slot appears.",
        }

    def check_all_intents(self) -> list[dict]:
        """Return status of all active intents for this agent."""
        return self.lmd.intent_list()


# ── Demo ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI concierge agent example")
    parser.add_argument("--api-key",      required=True)
    parser.add_argument("--wallet-id",    required=True)
    parser.add_argument("--city",         default="New York")
    parser.add_argument("--callback-url", default="")
    parser.add_argument("--name",         default="Demo User")
    parser.add_argument("--email",        default="demo@example.com")
    parser.add_argument("--phone",        default="+15550001234")
    args = parser.parse_args()

    concierge = ConciergeAgent(
        api_key=args.api_key,
        wallet_id=args.wallet_id,
        default_city=args.city,
    )
    customer = {"name": args.name, "email": args.email, "phone": args.phone}

    # Demonstrate multiple request types a real concierge would handle
    demo_requests = [
        "I need a massage in NYC tonight, budget $100",
        "Book me a yoga class in Chicago tomorrow morning",
        "Find a last-minute hotel in Miami under $200",
        "I want to go to a concert in Austin tonight",
    ]

    print("=" * 60)
    print("LastMinuteDeals Concierge Agent Demo")
    print("=" * 60)

    # Show live metrics first
    print("\n[System] Current system metrics:")
    metrics = concierge.lmd.metrics()
    inv = metrics.get("inventory", {})
    perf = metrics.get("performance", {})
    infra = metrics.get("infrastructure", {})
    print(f"  Live slots:      {inv.get('bookable_slots', 0):,} bookable")
    print(f"  Cities covered:  {inv.get('cities_covered', 0)}")
    print(f"  Success rate:    {perf.get('success_rate') or 'accumulating...'}")
    print(f"  Data freshness:  {infra.get('data_freshness_seconds', '?')}s ago")
    print(f"  Active intents:  {infra.get('active_intent_sessions', 0)}")

    print()

    # Process first demo request in detail, others briefly
    result = concierge.handle_request(demo_requests[0], customer, args.callback_url)
    print(f"\n[User response]: \"{result['message']}\"")

    print("\n" + "=" * 60)
    print("Other requests that would work:")
    for req in demo_requests[1:]:
        parsed = parse_intent(req)
        print(f"  \"{req}\"")
        print(f"    → category={parsed['category']}, city={parsed['city']}, urgency={parsed['urgency']}")


if __name__ == "__main__":
    main()
