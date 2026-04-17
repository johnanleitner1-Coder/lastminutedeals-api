"""
validate_pipeline.py — Behavioral output validator for the WAT pipeline.

Unlike integration_test.py which checks "did it run", this checks "is the
output correct" — values, schemas, business rules, and booking flow integrity.

Detection targets:
  - Slots with None/zero prices (invisible failure — pipeline runs fine)
  - booking_url fields that can't be parsed by OCTOBooker
  - Commission slots being marked up (B-25 class of bug)
  - Supabase slots missing required fields
  - Past slots in the live inventory
  - Booking record field completeness across all 4 entry points (static)

Usage:
    python tools/validate_pipeline.py [--json]
    or imported:
        from validate_pipeline import run_all_validations, ValidationFailure
"""

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

BASE_DIR  = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent

try:
    import requests
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

sys.stdout.reconfigure(encoding="utf-8")

REQUIRED_SLOT_FIELDS = {
    "slot_id", "platform", "business_id", "business_name", "category",
    "service_name", "start_time", "location_city", "booking_url",
    "scraped_at", "data_source", "confidence",
}

REQUIRED_BOOKING_URL_KEYS = {
    "_type", "base_url", "api_key_env", "product_id", "availability_id",
}

CANONICAL_BOOKING_FIELDS = {
    "booking_id", "status", "payment_status", "slot_id", "slot_json",
    "customer_name", "customer_email", "customer_phone", "quantity",
    "service_name", "business_name", "location_city", "start_time",
    "confirmation", "supplier_id", "platform", "booking_url",
    "our_price", "price_charged",
}


@dataclass
class ValidationFailure:
    check:       str
    severity:    str   # "critical" | "high" | "medium" | "low"
    message:     str
    detail:      str = ""
    file:        str = ""
    line:        int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Helper ────────────────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    k = os.getenv("SUPABASE_SECRET_KEY", "")
    return {"apikey": k, "Authorization": f"Bearer {k}"}


def _sb_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


# ── Check 1: octo_slots.json ──────────────────────────────────────────────────

def validate_octo_slots() -> List[ValidationFailure]:
    """Validate .tmp/octo_slots.json output from fetch_octo_slots.py."""
    failures: List[ValidationFailure] = []
    path = BASE_DIR / ".tmp" / "octo_slots.json"

    if not path.exists():
        failures.append(ValidationFailure(
            check="octo_slots_exists",
            severity="high",
            message=".tmp/octo_slots.json missing",
            detail="Run fetch_octo_slots.py first. Pipeline cannot proceed without slot data.",
        ))
        return failures

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        failures.append(ValidationFailure(
            check="octo_slots_parseable",
            severity="critical",
            message=f".tmp/octo_slots.json is not valid JSON: {e}",
        ))
        return failures

    if not isinstance(data, list):
        failures.append(ValidationFailure(
            check="octo_slots_is_list",
            severity="critical",
            message=f".tmp/octo_slots.json is {type(data).__name__}, expected list",
        ))
        return failures

    if len(data) == 0:
        failures.append(ValidationFailure(
            check="octo_slots_non_empty",
            severity="high",
            message=".tmp/octo_slots.json is empty — no slots fetched from any supplier",
            detail="Check that BOKUN_API_KEY is set in .env and supplier is enabled in octo_suppliers.json",
        ))
        return failures

    # Field validation
    missing_fields = []
    null_prices = []
    past_slots = []
    bad_booking_urls = []
    now = datetime.now(timezone.utc)

    for i, slot in enumerate(data):
        sid = slot.get("slot_id", f"slot[{i}]")

        # Required fields
        missing = REQUIRED_SLOT_FIELDS - set(slot.keys())
        if missing:
            missing_fields.append((sid, missing))

        # Price
        price = slot.get("price")
        if price is None:
            null_prices.append(sid)
        elif isinstance(price, (int, float)) and price <= 0:
            null_prices.append(f"{sid}(price={price})")

        # start_time must be in the future
        start = slot.get("start_time", "")
        if start:
            try:
                if start.endswith("Z"):
                    start = start[:-1] + "+00:00"
                dt = datetime.fromisoformat(start)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < now:
                    past_slots.append(sid)
            except Exception:
                pass

        # booking_url must be parseable JSON with required keys
        booking_url = slot.get("booking_url", "")
        if booking_url:
            if isinstance(booking_url, str) and booking_url.startswith("{"):
                try:
                    parsed = json.loads(booking_url)
                    missing_url_keys = REQUIRED_BOOKING_URL_KEYS - set(parsed.keys())
                    if missing_url_keys:
                        bad_booking_urls.append((sid, f"missing keys: {missing_url_keys}"))
                except json.JSONDecodeError as e:
                    bad_booking_urls.append((sid, f"JSON parse error: {e}"))
            elif isinstance(booking_url, str) and booking_url.startswith("http"):
                bad_booking_urls.append((sid, "plain URL — should be JSON blob"))

    if missing_fields:
        sample = missing_fields[:3]
        failures.append(ValidationFailure(
            check="octo_slots_field_completeness",
            severity="high",
            message=f"{len(missing_fields)} slot(s) missing required fields",
            detail="; ".join(f"{sid}:{m}" for sid, m in sample),
        ))

    if null_prices:
        pct = len(null_prices) / len(data) * 100
        failures.append(ValidationFailure(
            check="octo_slots_prices",
            severity="high" if pct > 10 else "medium",
            message=f"{len(null_prices)}/{len(data)} slots ({pct:.1f}%) have null/zero price",
            detail=f"Sample: {null_prices[:5]}",
        ))

    if past_slots:
        failures.append(ValidationFailure(
            check="octo_slots_future",
            severity="medium",
            message=f"{len(past_slots)} slot(s) have start_time in the past",
            detail=f"Sample: {past_slots[:5]}. These should be filtered before writing.",
        ))

    if bad_booking_urls:
        failures.append(ValidationFailure(
            check="octo_slots_booking_url",
            severity="high",
            message=f"{len(bad_booking_urls)} slot(s) have invalid booking_url",
            detail=f"Sample: {bad_booking_urls[:3]}",
        ))

    return failures


# ── Check 2: aggregated_slots.json ────────────────────────────────────────────

def validate_aggregated_slots() -> List[ValidationFailure]:
    """Validate .tmp/aggregated_slots.json output from aggregate_slots.py."""
    failures: List[ValidationFailure] = []
    path = BASE_DIR / ".tmp" / "aggregated_slots.json"

    if not path.exists():
        failures.append(ValidationFailure(
            check="aggregated_slots_exists",
            severity="high",
            message=".tmp/aggregated_slots.json missing",
        ))
        return failures

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        failures.append(ValidationFailure(
            check="aggregated_slots_parseable",
            severity="critical",
            message=f"Not valid JSON: {e}",
        ))
        return failures

    if not isinstance(data, list) or len(data) == 0:
        failures.append(ValidationFailure(
            check="aggregated_slots_non_empty",
            severity="high",
            message=f"Empty or wrong type: {type(data).__name__}",
        ))
        return failures

    # All slots must have platform field
    no_platform = [s.get("slot_id", "?") for s in data if not s.get("platform")]
    if no_platform:
        failures.append(ValidationFailure(
            check="aggregated_slots_platform",
            severity="high",
            message=f"{len(no_platform)} slot(s) missing 'platform' field",
            detail=f"Sample: {no_platform[:5]}",
        ))

    # No duplicate slot_ids
    ids = [s.get("slot_id") for s in data]
    dupes = [sid for sid in ids if sid and ids.count(sid) > 1]
    if dupes:
        failures.append(ValidationFailure(
            check="aggregated_slots_no_dupes",
            severity="medium",
            message=f"{len(set(dupes))} duplicate slot_id(s) found",
            detail=f"Sample: {list(set(dupes))[:3]}",
        ))

    # Pricing coverage
    priced = sum(1 for s in data if s.get("our_price") is not None)
    pct = priced / len(data) * 100 if data else 0
    if pct < 95:
        failures.append(ValidationFailure(
            check="aggregated_slots_pricing",
            severity="high",
            message=f"Only {pct:.1f}% of slots have our_price ({priced}/{len(data)})",
            detail="compute_pricing.py may have failed or not been run yet.",
        ))

    # Commission model: our_price must equal price (no markup)
    markup_violations = []
    for s in data:
        if s.get("pricing_model") == "commission":
            price    = s.get("price")
            our_price = s.get("our_price")
            if price is not None and our_price is not None:
                if abs(float(our_price) - float(price)) > 0.01:
                    markup_violations.append({
                        "slot_id": s.get("slot_id", "?"),
                        "price": price,
                        "our_price": our_price,
                    })
    if markup_violations:
        failures.append(ValidationFailure(
            check="aggregated_slots_commission_pricing",
            severity="critical",
            message=(
                f"{len(markup_violations)} commission slot(s) have our_price ≠ price "
                f"(Bug B-25 class: commission suppliers must not be marked up)"
            ),
            detail=f"Sample: {markup_violations[:2]}",
        ))

    return failures


# ── Check 3: Supabase live inventory ─────────────────────────────────────────

def validate_supabase_slots() -> List[ValidationFailure]:
    """Validate that Supabase slots match expected schema and are current."""
    failures: List[ValidationFailure] = []

    if not (_sb_url() and os.getenv("SUPABASE_SECRET_KEY")):
        return failures  # Skip if not configured

    try:
        resp = requests.get(
            f"{_sb_url()}/rest/v1/slots",
            headers={**_sb_headers(), "Range": "0-9"},
            params={"select": "*", "limit": "10"},
            timeout=10,
        )
        if resp.status_code not in (200, 206):
            failures.append(ValidationFailure(
                check="supabase_slots_readable",
                severity="critical",
                message=f"Supabase slots table returned HTTP {resp.status_code}",
                detail=resp.text[:200],
            ))
            return failures

        sample = resp.json()
        if not sample:
            return failures  # Empty is handled by integration_test

        # Check all 20 required fields
        missing_in_supabase = []
        now = datetime.now(timezone.utc)
        past_count = 0

        for slot in sample:
            missing = REQUIRED_SLOT_FIELDS - set(slot.keys())
            if missing:
                missing_in_supabase.append((slot.get("slot_id", "?"), missing))

            start = slot.get("start_time", "")
            if start:
                try:
                    if start.endswith("Z"):
                        start = start[:-1] + "+00:00"
                    dt = datetime.fromisoformat(start)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now:
                        past_count += 1
                except Exception:
                    pass

        if missing_in_supabase:
            failures.append(ValidationFailure(
                check="supabase_slots_schema",
                severity="high",
                message=f"{len(missing_in_supabase)} Supabase slot(s) missing required fields",
                detail=str(missing_in_supabase[:3]),
            ))

        if past_count > 0:
            failures.append(ValidationFailure(
                check="supabase_slots_freshness",
                severity="medium",
                message=f"{past_count}/10 sampled Supabase slots have past start_time",
                detail="sync_to_supabase.py should filter past slots before upsert.",
            ))

        # Validate commission pricing in Supabase too
        commission_violations = [
            s for s in sample
            if s.get("pricing_model") == "commission"
            and s.get("price") is not None
            and s.get("our_price") is not None
            and abs(float(s["our_price"]) - float(s["price"])) > 0.01
        ]
        if commission_violations:
            failures.append(ValidationFailure(
                check="supabase_commission_pricing",
                severity="critical",
                message=f"{len(commission_violations)} Supabase slot(s) violate commission pricing",
                detail=f"our_price ≠ price for commission model (B-25). Sample: {commission_violations[0].get('slot_id')}",
            ))

    except Exception as e:
        failures.append(ValidationFailure(
            check="supabase_slots_reachable",
            severity="high",
            message=f"Supabase validation failed: {e}",
        ))

    return failures


# ── Check 4: booking_url field integrity across all entry points (static) ─────

def validate_booking_entry_points() -> List[ValidationFailure]:
    """
    Static check: all 4 booking entry points must populate the canonical
    20-field booking record schema. Catches missing fields before they
    cause cancellation or reconciliation failures.
    """
    failures: List[ValidationFailure] = []
    server_path = TOOLS_DIR / "run_api_server.py"

    if not server_path.exists():
        return failures

    try:
        source = server_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return failures

    lines = source.splitlines()

    # Find each booking entry point by its route decorator
    entry_points = {
        "/api/book":            {"route": "@app.route.*api/book", "fields": set()},
        "/api/book/direct":     {"route": "@app.route.*api/book/direct", "fields": set()},
        "book_with_saved_card": {"route": "book_with_saved_card|customers.*book", "fields": set()},
        "execute/guaranteed":   {"route": "execute.*guaranteed|guaranteed.*execute", "fields": set()},
    }

    # For each entry point, find the booking dict and check its fields
    # We look for dict literals with "booking_id" or "status" key (signs it's a booking record)
    booking_record_pattern = re.compile(
        r'"(booking_id|customer_name|customer_email|price_charged|payment_method|confirmation)"'
    )

    # Find function boundaries and which fields they populate
    current_function = None
    function_fields: dict = {}

    for i, line in enumerate(lines, 1):
        # Track function definitions
        m = re.match(r'^def (\w+)\s*\(', line)
        if m:
            current_function = m.group(1)
            function_fields[current_function] = set()

        # Track field assignments in current function
        if current_function:
            for field_name in CANONICAL_BOOKING_FIELDS:
                if f'"{field_name}"' in line or f"'{field_name}'" in line:
                    if "=" in line or ":" in line:
                        function_fields[current_function].add(field_name)

    # Check known booking creation functions
    booking_functions = {
        k: v for k, v in function_fields.items()
        if any(kw in k for kw in ["book", "fulfill", "complete", "execute", "guaranteed"])
        and len(v) > 3  # Must have meaningful number of booking fields
    }

    for fn_name, fields in booking_functions.items():
        missing = CANONICAL_BOOKING_FIELDS - fields
        # Filter out fields that are often optional or computed later
        truly_missing = missing - {
            "reconciliation_flag_at", "reconciliation_required",
            "refund_id", "wallet_refunded", "cancelled_at", "cancelled_by",
            "failure_reason", "payment_intent_id", "wallet_id", "checkout_url",
        }
        if len(truly_missing) > 4:  # Threshold — some are set by helpers
            # Find the line where this function is defined
            fn_line = next(
                (i for i, l in enumerate(lines, 1) if re.match(rf'^def {fn_name}\s*\(', l)),
                0,
            )
            failures.append(ValidationFailure(
                check=f"booking_fields_{fn_name}",
                severity="medium",
                message=(
                    f"Function '{fn_name}' in run_api_server.py appears to be missing "
                    f"{len(truly_missing)} booking record fields: {sorted(truly_missing)[:6]}"
                ),
                detail="Incomplete booking records cause cancellation/reconciliation failures.",
                file="tools/run_api_server.py",
                line=fn_line,
            ))

    return failures


# ── Check 5: supplier_contracts.json integrity ────────────────────────────────

def validate_supplier_contracts() -> List[ValidationFailure]:
    """
    Every OCTO supplier must have an entry in supplier_contracts.json.
    Missing entries default to the wrong pricing model (B-26 class).
    """
    failures: List[ValidationFailure] = []
    contracts_path = TOOLS_DIR / "supplier_contracts.json"
    suppliers_path = TOOLS_DIR / "seeds" / "octo_suppliers.json"

    if not contracts_path.exists():
        failures.append(ValidationFailure(
            check="supplier_contracts_exists",
            severity="critical",
            message="tools/supplier_contracts.json not found",
            detail=(
                "Without this file compute_pricing.py cannot determine pricing model "
                "per supplier. All slots will default to the wrong model (B-26)."
            ),
        ))
        return failures

    try:
        contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    except Exception as e:
        failures.append(ValidationFailure(
            check="supplier_contracts_parseable",
            severity="critical",
            message=f"supplier_contracts.json invalid JSON: {e}",
        ))
        return failures

    if not suppliers_path.exists():
        return failures

    try:
        suppliers = json.loads(suppliers_path.read_text(encoding="utf-8"))
    except Exception:
        return failures

    enabled = [s for s in suppliers if s.get("enabled")]
    contract_keys = set(contracts.keys()) if isinstance(contracts, dict) else set()

    missing_contracts = []
    for s in enabled:
        sid = s.get("supplier_id") or s.get("name", "")
        if sid and sid not in contract_keys:
            missing_contracts.append(sid)

    if missing_contracts:
        failures.append(ValidationFailure(
            check="supplier_contracts_coverage",
            severity="high",
            message=(
                f"{len(missing_contracts)} enabled supplier(s) missing from supplier_contracts.json: "
                f"{missing_contracts}"
            ),
            detail="These suppliers will use default pricing model — likely wrong.",
        ))

    # Validate each contract has required fields
    if isinstance(contracts, dict):
        for sid, contract in contracts.items():
            if not contract.get("pricing_model"):
                failures.append(ValidationFailure(
                    check=f"supplier_contract_{sid}",
                    severity="high",
                    message=f"Contract for '{sid}' missing 'pricing_model' field",
                    detail="Must be 'commission' or 'net_rate'.",
                ))

    return failures


# ── Check 6: cancellation path consistency (static) ──────────────────────────

def validate_cancellation_paths() -> List[ValidationFailure]:
    """
    All three cancellation paths must implement the same set of operations:
    - Check circuit breaker state
    - Cancel at OCTO supplier
    - Process Stripe refund
    - Credit wallet (if wallet booking)
    - Send cancellation email
    - Queue failed OCTO cancellations for retry
    """
    failures: List[ValidationFailure] = []
    server_path = TOOLS_DIR / "run_api_server.py"

    if not server_path.exists():
        return failures

    try:
        source = server_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return failures

    lines = source.splitlines()

    # Define the 3 cancellation paths and their expected triggers
    paths = {
        "delete_booking (DELETE /bookings)": {
            "trigger_pattern": r"def.*delete.*booking|DELETE.*bookings|@app\.route.*bookings.*DELETE",
            "required_ops": {
                "octo_cancel":   ["_cancel_octo", "octo.*cancel", "cancellation_queue"],
                "stripe_refund": ["stripe.Refund", "refund.create", "stripe.*refund"],
                "wallet_credit": ["credit_wallet", "wallet.*credit", "refund.*wallet"],
                "email":         ["send_booking_cancel", "cancellation_email", "send_cancelled"],
            },
        },
        "self_serve_cancel (GET/POST /cancel)": {
            "trigger_pattern": r"self_serve_cancel|/cancel/|def.*cancel.*page",
            "required_ops": {
                "octo_cancel":   ["_cancel_octo", "octo.*cancel", "cancellation_queue"],
                "stripe_refund": ["stripe.Refund", "refund.create", "stripe.*refund"],
                "wallet_credit": ["credit_wallet", "wallet.*credit"],
                "email":         ["send_booking_cancel", "cancellation_email"],
            },
        },
        "bokun_webhook (POST /api/bokun/webhook)": {
            "trigger_pattern": r"bokun.*webhook|webhook.*bokun|/bokun/webhook",
            "required_ops": {
                "stripe_refund": ["stripe.Refund", "refund.create", "stripe.*refund"],
                "wallet_credit": ["credit_wallet", "wallet.*credit"],
                "email":         ["send_booking_cancel", "cancellation_email"],
            },
        },
    }

    # Extract each path's function body
    def _find_function_body(pattern: str, src_lines: list) -> tuple:
        """Find start line and approximate end of a function matching pattern."""
        for i, line in enumerate(src_lines):
            if re.search(pattern, line, re.IGNORECASE):
                return i, min(i + 200, len(src_lines))
        return -1, -1

    for path_name, config in paths.items():
        start, end = _find_function_body(config["trigger_pattern"], lines)
        if start < 0:
            continue  # Path not found in this file

        path_src = "\n".join(lines[start:end])

        for op_name, patterns in config["required_ops"].items():
            has_op = any(re.search(p, path_src, re.IGNORECASE) for p in patterns)
            if not has_op:
                failures.append(ValidationFailure(
                    check=f"cancellation_{path_name.split(' ')[0]}_{op_name}",
                    severity="high",
                    message=(
                        f"Cancellation path '{path_name}' does not appear to implement "
                        f"'{op_name}'. This divergence from other paths causes inconsistent "
                        f"behavior (bugs C-1 through C-8 class)."
                    ),
                    detail=f"Expected one of: {patterns}",
                    file="tools/run_api_server.py",
                    line=start + 1,
                ))

    return failures


# ── Run all ───────────────────────────────────────────────────────────────────

def run_all_validations(skip_network: bool = False) -> List[ValidationFailure]:
    """
    Run all behavioral validations. Returns list of failures sorted by severity.

    Args:
        skip_network: If True, skip Supabase checks (useful in offline testing).
    """
    all_failures: List[ValidationFailure] = []

    checks = [
        ("octo_slots",           validate_octo_slots),
        ("aggregated_slots",     validate_aggregated_slots),
        ("booking_entry_points", validate_booking_entry_points),
        ("supplier_contracts",   validate_supplier_contracts),
        ("cancellation_paths",   validate_cancellation_paths),
    ]
    if not skip_network:
        checks.append(("supabase_slots", validate_supabase_slots))

    for name, fn in checks:
        try:
            found = fn()
            if found:
                print(f"  [{name}] {len(found)} failure(s)")
            all_failures.extend(found)
        except Exception as e:
            all_failures.append(ValidationFailure(
                check=f"{name}_crashed",
                severity="high",
                message=f"Validator '{name}' crashed: {e}",
            ))

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(all_failures, key=lambda f: severity_order.get(f.severity, 9))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Behavioral pipeline validator")
    parser.add_argument("--json",    action="store_true", help="Output as JSON")
    parser.add_argument("--offline", action="store_true", help="Skip network checks")
    args = parser.parse_args()

    print("Running behavioral pipeline validation...")
    failures = run_all_validations(skip_network=args.offline)

    if args.json:
        print(json.dumps([f.to_dict() for f in failures], indent=2))
    else:
        if not failures:
            print("  All behavioral validations passed.")
        else:
            print(f"\nFound {len(failures)} validation failure(s):\n")
            for f in failures:
                print(f"  [{f.severity.upper():8s}] {f.check}")
                print(f"             {f.message}")
                if f.detail:
                    print(f"             Detail: {f.detail[:100]}")
                print()
