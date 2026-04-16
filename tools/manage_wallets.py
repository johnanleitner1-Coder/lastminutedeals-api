"""
manage_wallets.py — Agent wallet system for LastMinuteDeals.

Pre-funded accounts for AI agents. Eliminates the Stripe roundtrip on every
booking — agents deposit once, then call /execute/guaranteed and it books
instantly without redirect or per-booking payment friction.

Wallet lifecycle:
  1. Agent registers → gets wallet_id + api_key
  2. Agent funds wallet via Stripe Checkout link (one-time or recurring)
  3. Agent calls /execute/guaranteed with wallet_id → instant booking, instant debit
  4. Agent checks /api/wallets/{wallet_id}/balance for current balance

Wallet schema (.tmp/wallets.json):
  {
    "wlt_<24hex>": {
      "wallet_id":   "wlt_...",
      "api_key":     "lmd_...",
      "owner_name":  "MyAgent",
      "owner_email": "agent@example.com",
      "balance_cents": 5000,       // $50.00
      "currency":    "usd",
      "stripe_customer_id": "cus_...",
      "created_at":  "...",
      "last_funded": "...",
      "last_used":   "...",
      "transactions": [
        {"type": "credit", "amount_cents": 5000, "desc": "Top-up", "ts": "..."},
        {"type": "debit",  "amount_cents": 1200, "desc": "Booking: slot_xxx", "ts": "..."},
      ]
    }
  }

CLI:
  python tools/manage_wallets.py create --name "MyAgent" --email agent@example.com
  python tools/manage_wallets.py balance --wallet-id wlt_xxx
  python tools/manage_wallets.py topup   --wallet-id wlt_xxx --amount 50
  python tools/manage_wallets.py list
  python tools/manage_wallets.py transactions --wallet-id wlt_xxx
"""

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests as _req
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

WALLETS_FILE      = Path(".tmp/wallets.json")
_SB_WALLETS_PATH  = "config/wallets.json"  # path inside Supabase Storage bookings bucket
MIN_TOP_UP   = 500    # $5.00 minimum deposit (cents)
MAX_TOP_UP   = 500000 # $5,000.00 maximum deposit (cents)


# ── Persistence helpers ───────────────────────────────────────────────────────

def _sb_headers() -> dict:
    secret = os.getenv("SUPABASE_SECRET_KEY", "")
    return {"apikey": secret, "Authorization": f"Bearer {secret}"}

def _load_wallets() -> dict:
    """Load wallets. Primary: Supabase Storage (survives redeploys). Fallback: local cache."""
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            r = _req.get(
                f"{sb_url}/storage/v1/object/bookings/{_SB_WALLETS_PATH}",
                headers=_sb_headers(),
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                # Write local cache
                try:
                    WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    WALLETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
                return data
        except Exception as e:
            print(f"[WALLETS] Supabase load failed, using local cache: {e}")
    # Fallback: local cache
    if WALLETS_FILE.exists():
        try:
            return json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_wallets(wallets: dict) -> None:
    """Save wallets to both Supabase Storage (persistent) and local file (cache)."""
    # Local write always
    try:
        WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WALLETS_FILE.write_text(json.dumps(wallets, indent=2), encoding="utf-8")
    except Exception:
        pass
    # Supabase Storage write (survives redeploys)
    sb_url    = os.getenv("SUPABASE_URL", "").rstrip("/")
    sb_secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if sb_url and sb_secret:
        try:
            _req.post(
                f"{sb_url}/storage/v1/object/bookings/{_SB_WALLETS_PATH}",
                headers={**_sb_headers(), "Content-Type": "application/json", "x-upsert": "true"},
                data=json.dumps(wallets),
                timeout=8,
            )
        except Exception as e:
            print(f"[WALLETS] Supabase save failed (local write succeeded): {e}")

def _generate_wallet_id() -> str:
    return "wlt_" + secrets.token_hex(12)

def _generate_api_key() -> str:
    return "lmd_" + secrets.token_hex(24)


# ── Core wallet operations (used by execution_engine.py + run_api_server.py) ──

def get_wallet(wallet_id: str) -> dict | None:
    return _load_wallets().get(wallet_id)

def get_wallet_by_api_key(api_key: str) -> dict | None:
    for wlt in _load_wallets().values():
        if wlt.get("api_key") == api_key:
            return wlt
    return None

def get_balance(wallet_id: str) -> int | None:
    """Return balance in cents, or None if wallet not found."""
    wlt = get_wallet(wallet_id)
    return wlt.get("balance_cents") if wlt else None

def debit_wallet(wallet_id: str, amount_cents: int, description: str = "") -> bool:
    """
    Debit wallet by amount_cents. Returns True on success, False if insufficient funds.
    Thread-safe via file write (adequate for single-server deployment).
    """
    wallets = _load_wallets()
    wlt = wallets.get(wallet_id)
    if not wlt:
        raise ValueError(f"Wallet not found: {wallet_id}")

    current = wlt.get("balance_cents", 0)
    if current < amount_cents:
        raise ValueError(
            f"Insufficient wallet balance: have ${current/100:.2f}, need ${amount_cents/100:.2f}"
        )

    wlt["balance_cents"] = current - amount_cents
    wlt["last_used"] = datetime.now(timezone.utc).isoformat()
    wlt.setdefault("transactions", []).append({
        "type":         "debit",
        "amount_cents": amount_cents,
        "desc":         description or "Booking",
        "ts":           datetime.now(timezone.utc).isoformat(),
    })
    _save_wallets(wallets)
    return True

def credit_wallet(wallet_id: str, amount_cents: int, description: str = "Top-up") -> bool:
    """Credit wallet (used by Stripe webhook after successful payment)."""
    wallets = _load_wallets()
    wlt = wallets.get(wallet_id)
    if not wlt:
        return False

    wlt["balance_cents"] = wlt.get("balance_cents", 0) + amount_cents
    wlt["last_funded"] = datetime.now(timezone.utc).isoformat()
    wlt.setdefault("transactions", []).append({
        "type":         "credit",
        "amount_cents": amount_cents,
        "desc":         description,
        "ts":           datetime.now(timezone.utc).isoformat(),
    })
    _save_wallets(wallets)
    return True


# ── Wallet creation ───────────────────────────────────────────────────────────

def create_wallet(
    name: str,
    email: str,
    stripe_customer_id: str = "",
    spending_limit_cents: int | None = None,
) -> dict:
    """Create a new wallet. Returns the wallet record.

    spending_limit_cents: optional per-transaction cap. If set, any single booking
    that exceeds this amount will be rejected with failure_reason="spending_limit_exceeded".
    Useful for restricting untrusted agents. None = no limit.
    """
    wallets = _load_wallets()

    # Return existing wallet if email already registered
    for wlt in wallets.values():
        if wlt.get("owner_email") == email:
            return wlt

    wallet_id = _generate_wallet_id()
    api_key   = _generate_api_key()

    record = {
        "wallet_id":            wallet_id,
        "api_key":              api_key,
        "owner_name":           name,
        "owner_email":          email,
        "balance_cents":        0,
        "currency":             "usd",
        "stripe_customer_id":   stripe_customer_id,
        "spending_limit_cents": spending_limit_cents,  # None = no per-transaction cap
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "last_funded":          None,
        "last_used":            None,
        "transactions":         [],
    }

    wallets[wallet_id] = record
    _save_wallets(wallets)
    return record


def set_spending_limit(wallet_id: str, limit_cents: int | None) -> bool:
    """Set or remove the per-transaction spending limit on a wallet.

    limit_cents=None removes the limit entirely (unlimited).
    limit_cents=0 effectively blocks all autonomous bookings.
    Returns True on success, False if wallet not found.
    """
    wallets = _load_wallets()
    wlt = wallets.get(wallet_id)
    if not wlt:
        return False
    wlt["spending_limit_cents"] = limit_cents
    _save_wallets(wallets)
    return True


# ── Top-up Stripe payment link ────────────────────────────────────────────────

def create_topup_session(wallet_id: str, amount_cents: int) -> str:
    """
    Create a Stripe Checkout Session that funds the wallet on payment.
    Returns the checkout URL to redirect the agent/user to.
    """
    if amount_cents < MIN_TOP_UP:
        raise ValueError(f"Minimum deposit is ${MIN_TOP_UP/100:.2f}")
    if amount_cents > MAX_TOP_UP:
        raise ValueError(f"Maximum deposit is ${MAX_TOP_UP/100:.2f}")

    wlt = get_wallet(wallet_id)
    if not wlt:
        raise ValueError(f"Wallet not found: {wallet_id}")

    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not _stripe.api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")

    landing_url = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").rstrip("/")
    api_base    = os.getenv("BOOKING_SERVER_HOST", "http://localhost:5050").rstrip("/")

    # Ensure Stripe customer exists for this wallet
    cid = wlt.get("stripe_customer_id", "")
    if not cid:
        cust = _stripe.Customer.create(
            email=wlt["owner_email"],
            name=wlt["owner_name"],
            metadata={"wallet_id": wallet_id},
        )
        cid = cust["id"]
        wallets = _load_wallets()
        wallets[wallet_id]["stripe_customer_id"] = cid
        _save_wallets(wallets)

    session = _stripe.checkout.Session.create(
        customer=cid,
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency":     wlt.get("currency", "usd"),
                "product_data": {
                    "name":        "LastMinuteDeals Agent Wallet",
                    "description": f"Pre-funded credits for instant bookings — Wallet {wallet_id}",
                },
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{landing_url}?wallet=funded&wallet_id={wallet_id}",
        cancel_url=f"{landing_url}?wallet=cancelled",
        metadata={
            "wallet_id":    wallet_id,
            "amount_cents": str(amount_cents),
            "event_type":   "wallet_topup",
        },
    )
    return session.url


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Agent wallet management")
    sub = parser.add_subparsers(dest="cmd")

    # create
    p_create = sub.add_parser("create", help="Create a new wallet")
    p_create.add_argument("--name",  required=True)
    p_create.add_argument("--email", required=True)

    # balance
    p_bal = sub.add_parser("balance", help="Check wallet balance")
    p_bal.add_argument("--wallet-id", required=True)

    # topup
    p_topup = sub.add_parser("topup", help="Generate a top-up Stripe link")
    p_topup.add_argument("--wallet-id", required=True)
    p_topup.add_argument("--amount",    type=float, required=True, help="Amount in dollars (e.g. 50)")

    # list
    sub.add_parser("list", help="List all wallets")

    # transactions
    p_tx = sub.add_parser("transactions", help="Show wallet transactions")
    p_tx.add_argument("--wallet-id", required=True)

    # credit (admin)
    p_credit = sub.add_parser("credit", help="[Admin] Manually credit a wallet")
    p_credit.add_argument("--wallet-id", required=True)
    p_credit.add_argument("--amount",    type=float, required=True)
    p_credit.add_argument("--desc",      default="Manual credit")

    args = parser.parse_args()

    if args.cmd == "create":
        wlt = create_wallet(args.name, args.email)
        print(f"Wallet created:")
        print(f"  wallet_id: {wlt['wallet_id']}")
        print(f"  api_key:   {wlt['api_key']}")
        print(f"  balance:   $0.00")
        print(f"\nFund it: python tools/manage_wallets.py topup --wallet-id {wlt['wallet_id']} --amount 50")

    elif args.cmd == "balance":
        bal = get_balance(args.wallet_id)
        if bal is None:
            print(f"Wallet not found: {args.wallet_id}")
            sys.exit(1)
        print(f"Balance: ${bal/100:.2f}")

    elif args.cmd == "topup":
        amount_cents = int(args.amount * 100)
        try:
            url = create_topup_session(args.wallet_id, amount_cents)
            print(f"Top-up link (${args.amount:.2f}):")
            print(url)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif args.cmd == "list":
        wallets = _load_wallets()
        if not wallets:
            print("No wallets found.")
        for wlt in wallets.values():
            print(f"  {wlt['wallet_id']}  {wlt['owner_email']:40s}  ${wlt.get('balance_cents',0)/100:.2f}")

    elif args.cmd == "transactions":
        wlt = get_wallet(args.wallet_id)
        if not wlt:
            print(f"Wallet not found: {args.wallet_id}")
            sys.exit(1)
        txs = wlt.get("transactions", [])
        if not txs:
            print("No transactions.")
        for tx in txs[-20:]:
            sign = "+" if tx["type"] == "credit" else "-"
            print(f"  {tx['ts'][:19]}  {sign}${tx['amount_cents']/100:.2f}  {tx.get('desc','')}")

    elif args.cmd == "credit":
        amount_cents = int(args.amount * 100)
        ok = credit_wallet(args.wallet_id, amount_cents, args.desc)
        if ok:
            bal = get_balance(args.wallet_id)
            print(f"Credited ${args.amount:.2f}. New balance: ${bal/100:.2f}")
        else:
            print("Wallet not found.")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
