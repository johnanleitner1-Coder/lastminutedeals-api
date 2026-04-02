"""
wellness_booking_agent.py — Example: Autonomous wellness booking agent

Demonstrates how to use LastMinuteDeals as a booking backend.
This agent:
  1. Searches for last-minute wellness slots (massages, yoga, fitness)
  2. Picks the best value option
  3. Books it autonomously via wallet — no user redirect required
  4. Sends a callback when done

Run:
    python examples/wellness_booking_agent.py \\
        --city "New York" \\
        --budget 120 \\
        --api-key lmd_YOUR_KEY \\
        --wallet-id wlt_YOUR_WALLET

Or with delegated intent (fire-and-forget):
    python examples/wellness_booking_agent.py \\
        --city "New York" --budget 120 --api-key lmd_... --wallet-id wlt_... \\
        --intent --callback-url https://your-agent.com/webhook
"""

import argparse
import json
import sys
from pathlib import Path

# Add tools/ to path so we can import lmd_sdk from there
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from lmd_sdk import LastMinuteDeals, LastMinuteDealsError


def run_direct_booking(lmd: LastMinuteDeals, city: str, budget: float, customer: dict):
    """
    Synchronous path: search → pick best → execute guaranteed booking.
    Returns the booking result immediately.
    """
    print(f"[Agent] Searching for wellness slots in {city}, budget ${budget}...")

    slots = lmd.search(city=city, category="wellness", hours_ahead=48, max_price=budget)
    if not slots:
        print(f"[Agent] No wellness slots found in {city} under ${budget}.")
        return None

    print(f"[Agent] Found {len(slots)} slot(s). Top options:")
    for s in slots[:3]:
        print(f"  - {s.get('service_name')} @ {s.get('business_name', '?')} "
              f"| ${s.get('our_price', '?')} | {s.get('hours_until_start', '?'):.1f}h away")

    # Use /execute/best to let the engine pick optimally
    wallet_id = lmd.api_key  # illustrative — in real use pass wallet_id separately
    print(f"\n[Agent] Executing best-value booking via guaranteed engine...")

    # NOTE: real usage requires wallet_id from wallet_create()
    # This demo shows the call pattern; swap in your real wallet_id
    result = lmd.best(
        goal="maximize_value",
        city=city,
        category="wellness",
        budget=budget,
        hours_ahead=48,
        customer=customer,
        wallet_id=wallet_id,  # replace with real wlt_... ID
        explain=True,
    )

    if result.get("success"):
        print(f"\n[Agent] Booking confirmed!")
        print(f"  Confirmation:   {result.get('confirmation')}")
        print(f"  Service:        {result.get('service_name')}")
        print(f"  Platform:       {result.get('platform')}")
        print(f"  Price charged:  ${result.get('price_charged')}")
        print(f"  Savings:        ${result.get('savings_vs_market', 0):.2f}")
        print(f"  Attempts:       {result.get('attempts')} ({result.get('fallbacks_used')} fallback(s) used)")
        print(f"  Confidence:     {result.get('confidence_score', 0):.0%}")
        if result.get("explanation"):
            print(f"  Why chosen:     {result['explanation']}")
    else:
        print(f"\n[Agent] Booking failed: {result.get('error')}")
        print(f"  Attempts made: {result.get('attempts', 0)}")

    return result


def run_delegated_intent(lmd: LastMinuteDeals, city: str, budget: float, customer: dict,
                          wallet_id: str, callback_url: str = ""):
    """
    Async path: create a persistent intent and let the system handle it.
    Returns immediately. System will book autonomously and notify via callback_url.
    """
    print(f"[Agent] Creating persistent intent for wellness in {city}, budget ${budget}...")

    intent = lmd.intent(
        goal="find_and_book",
        category="wellness",
        city=city,
        budget=budget,
        hours_ahead=48,
        customer=customer,
        wallet_id=wallet_id,
        autonomy="full",      # auto-execute without asking
        callback_url=callback_url,
        ttl_hours=12,         # give up after 12 hours if nothing found
    )

    print(f"\n[Agent] Intent created — system is now working on it:")
    print(f"  Intent ID:  {intent.get('intent_id')}")
    print(f"  Status:     {intent.get('status')}")
    print(f"  Expires:    {intent.get('expires_at', '')[:19]}")
    print(f"  Message:    {intent.get('message')}")

    if callback_url:
        print(f"\n[Agent] You'll receive a POST to {callback_url} when booking completes.")
    else:
        print(f"\n[Agent] Poll status: GET /intent/{intent.get('intent_id')}")

    return intent


def show_market_intelligence(lmd: LastMinuteDeals, city: str):
    """Show what the system knows about the wellness market in this city."""
    print(f"\n[Agent] Pulling market intelligence for wellness/{city}...")
    data = lmd.insights(category="wellness", city=city)

    live = (data.get("live_inventory") or {}).get("wellness", {})
    if live:
        print(f"  Live slots:      {live.get('slot_count', 0)}")
        print(f"  Avg price:       ${live.get('avg_price', 0):.2f}")
        print(f"  Next slot in:    {live.get('next_slot_hours', '?')}h")

    fv = (data.get("fill_velocity") or {}).get("wellness", {})
    if fv:
        print(f"  Avg fill time:   {fv.get('avg_fill_minutes', '?')} min (slots sell out this fast)")

    window = data.get("best_booking_window")
    if window:
        print(f"  Best book time:  {window} before start")

    demand = data.get("active_demand_signals", 0)
    if demand:
        print(f"  Competing demand: {demand} other agent(s) hunting wellness in this city")


def main():
    parser = argparse.ArgumentParser(description="Wellness booking agent example")
    parser.add_argument("--city",         default="New York")
    parser.add_argument("--budget",       type=float, default=120.0)
    parser.add_argument("--api-key",      required=True, help="Your lmd_... API key")
    parser.add_argument("--wallet-id",    default="",    help="Your wlt_... wallet ID for guaranteed/intent execution")
    parser.add_argument("--intent",       action="store_true", help="Use delegated intent mode (async)")
    parser.add_argument("--callback-url", default="",    help="Webhook URL for intent notifications")
    parser.add_argument("--name",         default="Demo User")
    parser.add_argument("--email",        default="demo@example.com")
    parser.add_argument("--phone",        default="+15550001234")
    args = parser.parse_args()

    lmd = LastMinuteDeals(api_key=args.api_key)
    customer = {"name": args.name, "email": args.email, "phone": args.phone}

    # Always show market intelligence first
    show_market_intelligence(lmd, args.city)

    print()

    if args.intent:
        if not args.wallet_id:
            print("Error: --wallet-id required for intent mode. Create one at POST /api/wallets/create")
            sys.exit(1)
        run_delegated_intent(lmd, args.city, args.budget, customer, args.wallet_id, args.callback_url)
    else:
        run_direct_booking(lmd, args.city, args.budget, customer)


if __name__ == "__main__":
    main()
