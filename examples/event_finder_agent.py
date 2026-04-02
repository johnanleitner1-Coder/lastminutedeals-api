"""
event_finder_agent.py — Example: Last-minute event finder + booking agent

Searches for entertainment events happening in the next 12 hours,
ranks them by value, and books the best one autonomously.

Demonstrates:
  - Using /insights/market to make a smarter choice before searching
  - Using /execute/best with goal="minimize_wait" to get the soonest event
  - Polling intent status until completion (no webhook needed)

Run:
    python examples/event_finder_agent.py \\
        --city Chicago \\
        --api-key lmd_... \\
        --wallet-id wlt_...
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from lmd_sdk import LastMinuteDeals, LastMinuteDealsError


def find_and_book_event(lmd: LastMinuteDeals, city: str, budget: float, customer: dict, wallet_id: str):
    print(f"[EventAgent] Finding last-minute events in {city} (next 12 hours)...")

    # Step 1: Check market intelligence — what's the success rate for entertainment here?
    insights = lmd.insights(category="entertainment", city=city)
    plat_rel  = insights.get("platform_reliability", {})
    if plat_rel:
        best_platform = max(plat_rel.items(), key=lambda x: x[1].get("success_rate", 0))
        print(f"[EventAgent] Most reliable platform: {best_platform[0]} "
              f"({best_platform[1].get('success_rate', 0):.0%} success rate)")

    # Step 2: Quick search to show what's available
    slots = lmd.search(city=city, category="entertainment", hours_ahead=12, max_price=budget)
    print(f"[EventAgent] {len(slots)} event(s) found in next 12 hours:")
    for s in slots[:5]:
        h = s.get("hours_until_start", 0) or 0
        print(f"  {h:4.1f}h  ${s.get('our_price', '?'):>6}  {s.get('service_name', '?')[:50]}")

    if not slots:
        print("[EventAgent] No events found. Creating a monitoring intent instead...")
        intent = lmd.intent(
            goal="find_and_book",
            category="entertainment",
            city=city,
            budget=budget,
            hours_ahead=24,      # widen to 24h for the intent
            customer=customer,
            wallet_id=wallet_id,
            autonomy="full",
            ttl_hours=6,
        )
        print(f"[EventAgent] Intent created: {intent['intent_id']} — will auto-book when events appear.")
        return

    # Step 3: Execute with goal="minimize_wait" — get there soonest
    print(f"\n[EventAgent] Booking soonest available event (goal: minimize_wait)...")
    result = lmd.best(
        goal="minimize_wait",
        city=city,
        category="entertainment",
        budget=budget,
        hours_ahead=12,
        customer=customer,
        wallet_id=wallet_id,
        explain=True,
    )

    if result.get("success"):
        print(f"\n[EventAgent] Booked!")
        print(f"  Event:        {result.get('service_name')}")
        print(f"  Confirmation: {result.get('confirmation')}")
        print(f"  Price:        ${result.get('price_charged')}")
        print(f"  Why chosen:   {result.get('explanation', '')}")
    else:
        print(f"\n[EventAgent] Could not complete booking: {result.get('error')}")
        print(f"  Tried {result.get('attempts', 0)} option(s).")


def poll_intent_until_done(lmd: LastMinuteDeals, intent_id: str, max_wait_seconds: int = 300):
    """
    Poll an intent session until it resolves (or timeout).
    In production, use callback_url instead of polling.
    """
    print(f"\n[EventAgent] Polling intent {intent_id} (max {max_wait_seconds}s)...")
    start = time.time()
    while time.time() - start < max_wait_seconds:
        status = lmd.intent_status(intent_id)
        state  = status.get("status", "unknown")
        print(f"  Status: {state} (attempts: {status.get('attempt_count', 0)})")

        if state in ("completed", "failed", "expired", "cancelled"):
            if state == "completed":
                result = status.get("result", {})
                print(f"\n[EventAgent] Booking completed!")
                print(f"  Confirmation: {result.get('confirmation')}")
                print(f"  Service:      {result.get('service_name')}")
                print(f"  Price:        ${result.get('price_charged')}")
            else:
                print(f"\n[EventAgent] Intent ended with status: {state}")
            return status

        time.sleep(15)  # poll every 15 seconds

    print(f"[EventAgent] Timeout after {max_wait_seconds}s. Intent still running.")
    return None


def main():
    parser = argparse.ArgumentParser(description="Event finder agent example")
    parser.add_argument("--city",      default="Chicago")
    parser.add_argument("--budget",    type=float, default=80.0)
    parser.add_argument("--api-key",   required=True)
    parser.add_argument("--wallet-id", required=True)
    parser.add_argument("--name",      default="Demo User")
    parser.add_argument("--email",     default="demo@example.com")
    parser.add_argument("--phone",     default="+15550001234")
    parser.add_argument("--poll",      action="store_true", help="If intent created, poll until done")
    args = parser.parse_args()

    lmd      = LastMinuteDeals(api_key=args.api_key)
    customer = {"name": args.name, "email": args.email, "phone": args.phone}

    find_and_book_event(lmd, args.city, args.budget, customer, args.wallet_id)


if __name__ == "__main__":
    main()
