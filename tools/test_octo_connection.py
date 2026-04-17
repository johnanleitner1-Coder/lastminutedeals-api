"""
test_octo_connection.py — Verify API keys and connectivity for OCTO + Rezdy platforms.

Run this immediately after adding a new API key to .env to confirm everything
works before running the full pipeline.

Usage:
    python tools/test_octo_connection.py              # test all configured platforms
    python tools/test_octo_connection.py --platform ventrata
    python tools/test_octo_connection.py --platform rezdy
    python tools/test_octo_connection.py --platform bokun
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

BASE_DIR    = Path(__file__).parent.parent
SEEDS_FILE  = BASE_DIR / "tools" / "seeds" / "octo_suppliers.json"

PASS = "✓"
FAIL = "✗"
SKIP = "–"


# ── OCTO connection test ──────────────────────────────────────────────────────

def test_octo_supplier(supplier: dict) -> dict:
    """
    Test one OCTO supplier: verify API key, list products, check availability on one product.
    Returns a result dict: {passed, name, products, sample_slot, error}
    """
    name        = supplier["name"]
    base_url    = supplier["base_url"].rstrip("/")
    api_key_env = supplier["api_key_env"]
    api_key     = os.getenv(api_key_env, "").strip()

    if not api_key:
        return {
            "passed":  None,
            "name":    name,
            "note":    f"SKIPPED — {api_key_env} not set in .env",
            "products": 0,
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        # NOTE: Octo-Capabilities header intentionally omitted from /products calls.
        # Bokun hangs on /products when this header is present. Add it only on /availability.
    }

    # ── Step 1: GET /products ─────────────────────────────────────────────────
    try:
        resp = requests.get(f"{base_url}/products", headers=headers, timeout=15)
        if resp.status_code == 401:
            return {"passed": False, "name": name,
                    "error": f"401 Unauthorized — check {api_key_env} value"}
        if resp.status_code == 403:
            return {"passed": False, "name": name,
                    "error": f"403 Forbidden — key may not have reseller permissions"}
        resp.raise_for_status()
        products = resp.json()
    except requests.HTTPError as exc:
        return {"passed": False, "name": name,
                "error": f"GET /products failed: {exc.response.status_code} {exc.response.text[:150]}"}
    except Exception as exc:
        return {"passed": False, "name": name, "error": f"GET /products error: {exc}"}

    if not products:
        return {
            "passed":   True,
            "name":     name,
            "products": 0,
            "note":     "Connected successfully but no products returned. "
                        "This is normal for a new account with no approved supplier relationships.",
        }

    # ── Step 2: POST /availability on first product ───────────────────────────
    product    = products[0]
    product_id = product.get("id", "")
    options    = product.get("options") or []
    option_id  = options[0].get("id", "DEFAULT") if options else "DEFAULT"
    units      = options[0].get("units") or [] if options else []
    unit_id    = units[0].get("id", "adult") if units else "adult"

    now        = datetime.now(timezone.utc)
    date_start = now.strftime("%Y-%m-%d")
    date_end   = (now + timedelta(days=4)).strftime("%Y-%m-%d")

    avail_payload = {
        "productId":      product_id,
        "optionId":       option_id,
        "localDateStart": date_start,
        "localDateEnd":   date_end,
        "units":          [{"id": unit_id, "quantity": 1}],
    }

    sample_slot = None
    avail_count = 0
    try:
        resp = requests.post(
            f"{base_url}/availability",
            headers=headers,
            json=avail_payload,
            timeout=15,
        )
        resp.raise_for_status()
        availability = resp.json()
        avail_count  = len(availability)
        if availability:
            a = availability[0]
            sample_slot = (
                f"{product.get('internalName') or product_id} — "
                f"{a.get('localDateTimeStart', '')[:16]} "
                f"({a.get('status', '')}, vacancies={a.get('vacancies', '?')})"
            )
    except Exception as exc:
        sample_slot = f"availability check failed: {exc}"

    return {
        "passed":      True,
        "name":        name,
        "products":    len(products),
        "avail_count": avail_count,
        "sample_slot": sample_slot,
    }


# ── Rezdy connection test ─────────────────────────────────────────────────────

def test_rezdy() -> dict:
    api_key = os.getenv("REZDY_API_KEY", "").strip()

    if not api_key:
        return {"passed": None, "name": "Rezdy", "note": "SKIPPED — REZDY_API_KEY not set in .env"}

    base_url = "https://api.rezdy.com/v1"

    # ── Step 1: GET /products ─────────────────────────────────────────────────
    try:
        resp = requests.get(
            f"{base_url}/products",
            params={"apiKey": api_key, "limit": 10},
            timeout=15,
        )
        if resp.status_code == 401:
            return {"passed": False, "name": "Rezdy",
                    "error": "401 Unauthorized — check REZDY_API_KEY value"}
        if resp.status_code == 403:
            return {"passed": False, "name": "Rezdy",
                    "error": "403 Forbidden — key may be pending approval"}
        resp.raise_for_status()
        data     = resp.json()
        products = data.get("products") or []
    except Exception as exc:
        return {"passed": False, "name": "Rezdy", "error": f"GET /products error: {exc}"}

    if not products:
        return {
            "passed":   True,
            "name":     "Rezdy",
            "products": 0,
            "note":     "Connected successfully but no products returned. "
                        "This is normal until suppliers grant you rate access. "
                        "Use the Rezdy Marketplace to request access from suppliers.",
        }

    # ── Step 2: GET /availability on first product ────────────────────────────
    product      = products[0]
    product_code = product.get("productCode", "")
    now          = datetime.now(timezone.utc)
    start_str    = now.strftime("%Y-%m-%d %H:%M:%S")
    end_str      = (now + timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")

    sample_slot  = None
    avail_count  = 0
    try:
        resp = requests.get(
            f"{base_url}/availability",
            params={
                "apiKey":      api_key,
                "productCode": product_code,
                "startTime":   start_str,
                "endTime":     end_str,
                "qty":         1,
            },
            timeout=15,
        )
        if resp.ok:
            sessions    = resp.json().get("sessions") or []
            avail_count = len(sessions)
            if sessions:
                s = sessions[0]
                sample_slot = (
                    f"{product.get('name') or product_code} — "
                    f"{s.get('startTimeLocal', '')[:16]} "
                    f"(seats={s.get('seatsAvailable', '?')})"
                )
    except Exception as exc:
        sample_slot = f"availability check failed: {exc}"

    return {
        "passed":      True,
        "name":        "Rezdy",
        "products":    len(products),
        "avail_count": avail_count,
        "sample_slot": sample_slot,
    }


# ── Print helpers ─────────────────────────────────────────────────────────────

def _print_result(result: dict) -> None:
    name   = result["name"]
    passed = result.get("passed")

    if passed is None:
        icon = SKIP
        print(f"  {icon} {name}: {result.get('note', 'skipped')}")
        return

    if not passed:
        icon = FAIL
        print(f"  {icon} {name}: FAILED — {result.get('error', 'unknown error')}")
        return

    icon   = PASS
    parts  = [f"{result.get('products', 0)} products"]
    if result.get("avail_count") is not None:
        parts.append(f"{result['avail_count']} availability slots in next 4 days")
    print(f"  {icon} {name}: CONNECTED — {', '.join(parts)}")
    if result.get("sample_slot"):
        print(f"      Sample: {result['sample_slot']}")
    if result.get("note"):
        print(f"      Note: {result['note']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test OCTO + Rezdy API connectivity")
    parser.add_argument("--platform", help="Test only this platform (ventrata, rezdy, bokun, etc.)")
    args = parser.parse_args()

    print(f"\nLastMinuteDeals — API Connection Test")
    print(f"{'='*50}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    results  = []
    passed   = 0
    failed   = 0
    skipped  = 0

    # ── Load OCTO supplier configs ────────────────────────────────────────────
    if SEEDS_FILE.exists():
        suppliers = json.loads(SEEDS_FILE.read_text(encoding="utf-8"))
        octo_suppliers = [
            s for s in suppliers
            if not s.get("api_format")   # only pure OCTO suppliers
            and (not args.platform or args.platform.lower() in s["supplier_id"].lower())
        ]
    else:
        print(f"WARNING: {SEEDS_FILE} not found — skipping OCTO supplier tests")
        octo_suppliers = []

    print("OCTO suppliers:")
    for supplier in octo_suppliers:
        result = test_octo_supplier(supplier)
        results.append(result)
        _print_result(result)
        if result["passed"] is True:
            passed += 1
        elif result["passed"] is False:
            failed += 1
        else:
            skipped += 1
        time.sleep(0.3)

    # ── Rezdy ─────────────────────────────────────────────────────────────────
    if not args.platform or args.platform.lower() == "rezdy":
        print("\nRezdy:")
        result = test_rezdy()
        results.append(result)
        _print_result(result)
        if result["passed"] is True:
            passed += 1
        elif result["passed"] is False:
            failed += 1
        else:
            skipped += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

    if failed > 0:
        print(f"\n{FAIL} {failed} platform(s) failed — check API keys in .env")
        sys.exit(1)
    elif passed == 0:
        print(f"\n{SKIP} No platforms configured yet.")
        print("Add API keys to .env — see .env.example for instructions.")
        print("Fastest start: VENTRATA_API_KEY (test key, no signup needed)")
        sys.exit(0)
    else:
        print(f"\n{PASS} All configured platforms connected successfully.")
        print("Run the full fetch: python tools/fetch_octo_slots.py")
        sys.exit(0)


if __name__ == "__main__":
    main()
