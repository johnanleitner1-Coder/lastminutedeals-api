"""
execution_engine.py — Guaranteed execution engine for LastMinuteDeals.

Implements the retry + multi-path strategy that makes /execute/guaranteed reliable:

  Attempt 1:  Original requested slot
  Attempt 2:  Same slot, second booking attempt (transient failure retry)
  Attempt 3:  Best similar slot — same category + city, within ±2h start time
  Attempt 4:  Any slot — same category + city, relaxed time window
  Attempt 5:  Different platform — same category + city (expands platform set)
  Attempt 6:  Broaden city (metro area match)
  Attempt 7:  Alternative category (if allow_alternatives=True)

After each failed attempt, the engine logs the failure and tries the next path.
Returns an ExecutionResult with full audit trail.

Usage:
    from tools.execution_engine import ExecutionEngine, ExecutionRequest

    engine = ExecutionEngine(slots=load_slots(), booked_ids=load_booked())
    result = engine.execute(ExecutionRequest(
        slot_id="abc123",             # optional — if None, engine finds the best match
        category="wellness",
        city="New York",
        hours_ahead=24,
        budget=150.0,
        customer={"name": "...", "email": "...", "phone": "..."},
        allow_alternatives=True,
        payment_method="wallet",       # "wallet" | "stripe_pi" | "stripe_checkout"
        wallet_id="wlt_...",           # required if payment_method="wallet"
        payment_intent_id="pi_...",    # required if payment_method="stripe_pi"
    ))
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT      = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent

sys.stdout.reconfigure(encoding="utf-8")


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ExecutionRequest:
    customer: dict                   # name, email, phone
    slot_id: str | None     = None   # if given, try this slot first
    category: str           = ""
    city: str               = ""
    hours_ahead: int        = 24
    budget: float | None    = None
    allow_alternatives: bool = True
    payment_method: str     = "stripe_checkout"  # "wallet" | "stripe_pi" | "stripe_checkout"
    wallet_id: str | None   = None
    payment_intent_id: str | None = None


@dataclass
class AttemptRecord:
    attempt: int
    strategy: str
    slot_id: str
    service_name: str
    platform: str
    price: float
    outcome: str          # "success" | "unavailable" | "booking_failed" | "payment_failed"
    confirmation: str     = ""
    error: str            = ""


@dataclass
class ExecutionResult:
    success: bool
    status: str           # "booked" | "failed" | "no_slots"
    confirmation: str     = ""
    slot_id: str          = ""
    service_name: str     = ""
    platform: str         = ""
    price_charged: float  = 0.0
    attempts: int         = 0
    fallbacks_used: int   = 0
    savings_vs_market: float = 0.0
    confidence_score: float  = 0.0
    attempt_log: list[AttemptRecord] = field(default_factory=list)
    error: str            = ""

    def to_dict(self) -> dict:
        return {
            "success":           self.success,
            "status":            self.status,
            "confirmation":      self.confirmation,
            "slot_id":           self.slot_id,
            "service_name":      self.service_name,
            "platform":          self.platform,
            "price_charged":     self.price_charged,
            "attempts":          self.attempts,
            "fallbacks_used":    self.fallbacks_used,
            "savings_vs_market": self.savings_vs_market,
            "confidence_score":  self.confidence_score,
            "attempt_log": [
                {
                    "attempt":      a.attempt,
                    "strategy":     a.strategy,
                    "slot_id":      a.slot_id,
                    "service_name": a.service_name,
                    "platform":     a.platform,
                    "price":        a.price,
                    "outcome":      a.outcome,
                    "confirmation": a.confirmation,
                    "error":        a.error,
                }
                for a in self.attempt_log
            ],
            "error": self.error,
        }


# ── Execution engine ──────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Multi-path booking execution engine.

    Loads slots from aggregated_slots.json (or accepts a pre-loaded list).
    Calls complete_booking.py for each attempt.
    Manages wallet debits / Stripe capture on success.
    """

    MAX_ATTEMPTS = 7

    def __init__(self, slots: list[dict] | None = None, booked_ids: set | None = None):
        self.slots: list[dict] = slots if slots is not None else self._load_slots()
        self.booked_ids: set   = booked_ids if booked_ids is not None else set()
        self._complete_booking_module = None

    # ── Slot loading ───────────────────────────────────────────────────────────

    @staticmethod
    def _load_slots() -> list[dict]:
        agg = ROOT / ".tmp" / "aggregated_slots.json"
        if agg.exists():
            try:
                return json.loads(agg.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    # ── Candidate selection ───────────────────────────────────────────────────

    def _available_slots(self) -> list[dict]:
        """Return slots that are priced, not booked, and not expired."""
        now = datetime.now(timezone.utc)
        out = []
        for s in self.slots:
            if s.get("slot_id") in self.booked_ids:
                continue
            h = s.get("hours_until_start")
            if h is not None and h < 0:
                continue
            p = float(s.get("our_price") or s.get("price") or 0)
            if p <= 0:
                continue
            out.append(s)
        return out

    def _score_similarity(self, slot: dict, req: ExecutionRequest) -> float:
        """Score 0-1 how well a slot matches the request (1 = perfect match)."""
        score = 0.0

        if req.category and slot.get("category") == req.category:
            score += 0.4
        elif req.category and req.category.lower() in (slot.get("category") or "").lower():
            score += 0.2

        city_match = req.city.lower() if req.city else ""
        slot_city  = (slot.get("location_city") or "").lower()
        if city_match and city_match in slot_city:
            score += 0.3
        elif city_match and slot_city in city_match:
            score += 0.2
        elif not city_match:
            score += 0.3  # no city constraint → all cities equally valid

        h = slot.get("hours_until_start") or 999
        if req.hours_ahead and h <= req.hours_ahead:
            score += 0.2
        elif h <= 72:
            score += 0.1

        if req.budget:
            p = float(slot.get("our_price") or slot.get("price") or 0)
            if p <= req.budget:
                score += 0.1

        return min(score, 1.0)

    def _find_candidates(self, req: ExecutionRequest, strategy: str) -> list[dict]:
        """
        Return ranked slot candidates for a given strategy.

        Strategies:
          "exact"       — the specific slot_id in req
          "similar"     — same cat + city, within 2h of original start
          "category_city" — same cat + city, any time in window
          "any_platform"  — same cat + city, any platform
          "metro"         — broaden city (partial match)
          "alternatives"  — relax category
        """
        available = self._available_slots()

        if strategy == "exact":
            if not req.slot_id:
                return []
            return [s for s in available if s.get("slot_id") == req.slot_id]

        if strategy == "similar" and req.slot_id:
            original = next((s for s in self.slots if s.get("slot_id") == req.slot_id), None)
            orig_h   = (original or {}).get("hours_until_start", 0)
            return sorted(
                [
                    s for s in available
                    if s.get("slot_id") != req.slot_id
                    and s.get("category") == (original or {}).get("category")
                    and (s.get("location_city") or "").lower() == ((original or {}).get("location_city") or "").lower()
                    and abs((s.get("hours_until_start") or 999) - orig_h) <= 2
                ],
                key=lambda s: self._score_similarity(s, req),
                reverse=True,
            )

        if strategy == "category_city":
            return sorted(
                [
                    s for s in available
                    if (not req.category or s.get("category") == req.category)
                    and (not req.city or req.city.lower() in (s.get("location_city") or "").lower())
                    and (s.get("hours_until_start") or 999) <= req.hours_ahead
                ],
                key=lambda s: self._score_similarity(s, req),
                reverse=True,
            )

        if strategy == "any_platform":
            # Same as category_city but explicitly ignores platform filter
            return self._find_candidates(req, "category_city")

        if strategy == "metro":
            # Partial city match — catches "New York" vs "NYC" vs "Brooklyn"
            city_tokens = set(req.city.lower().split()) if req.city else set()
            return sorted(
                [
                    s for s in available
                    if (not req.category or s.get("category") == req.category)
                    and (not city_tokens or any(t in (s.get("location_city") or "").lower() for t in city_tokens))
                    and (s.get("hours_until_start") or 999) <= req.hours_ahead
                ],
                key=lambda s: self._score_similarity(s, req),
                reverse=True,
            )

        if strategy == "alternatives":
            return sorted(
                [s for s in available if (s.get("hours_until_start") or 999) <= req.hours_ahead],
                key=lambda s: self._score_similarity(s, req),
                reverse=True,
            )

        return []

    # ── Booking execution ─────────────────────────────────────────────────────

    def _load_complete_booking(self):
        if self._complete_booking_module:
            return self._complete_booking_module
        path = TOOLS_DIR / "complete_booking.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location("complete_booking", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._complete_booking_module = mod
        return mod

    def _attempt_booking(self, slot: dict, customer: dict) -> str:
        """
        Try to book slot. Returns confirmation string on success.
        Raises on failure (BookingError subclasses or generic Exception).
        """
        mod = self._load_complete_booking()
        if not mod:
            raise RuntimeError("complete_booking.py not available — Playwright not installed")

        result = mod.complete_booking(
            slot_id=slot.get("slot_id", ""),
            customer=customer,
            platform=slot.get("platform", ""),
            booking_url=slot.get("booking_url", ""),
        )

        # OCTOBooker.run() returns a dict with "confirmation", "supplier_reference",
        # "booking_meta" keys. Other bookers return a plain string confirmation.
        if isinstance(result, dict):
            confirmation = result.get("confirmation", "")
            # Store supplier_reference and booking_meta on the slot for downstream use
            slot["_supplier_reference"] = result.get("supplier_reference", "")
            slot["_booking_meta"]       = result.get("booking_meta", {})
            return str(confirmation)

        return result

    # ── Payment execution ─────────────────────────────────────────────────────

    def _charge_wallet(self, wallet_id: str, amount_cents: int, slot_id: str) -> bool:
        """Debit a pre-funded wallet. Returns True on success."""
        from tools.manage_wallets import debit_wallet
        try:
            return debit_wallet(wallet_id, amount_cents, description=f"Booking: {slot_id}")
        except Exception as e:
            print(f"[ENGINE] Wallet debit failed: {e}")
            return False

    def _capture_stripe(self, payment_intent_id: str) -> bool:
        """Capture an existing Stripe PaymentIntent (manual-capture mode)."""
        try:
            import os, stripe
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
            stripe.PaymentIntent.capture(payment_intent_id)
            return True
        except Exception as e:
            print(f"[ENGINE] Stripe capture failed: {e}")
            return False

    def _cancel_stripe(self, payment_intent_id: str):
        """Cancel a Stripe hold — customer never charged."""
        try:
            import os, stripe
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
            stripe.PaymentIntent.cancel(payment_intent_id)
        except Exception as e:
            print(f"[ENGINE] Stripe hold cancel failed for {payment_intent_id}: {e} — "
                  "manual review required to ensure customer was not charged")

    def _cancel_octo(self, platform: str, confirmation: str) -> bool:
        """
        Cancel a confirmed OCTO booking when payment fails after booking succeeded.
        Best-effort: logs failure but does not raise. Returns True if cancelled.
        """
        import json as _json, os, requests as _req
        seeds_path = TOOLS_DIR / "seeds" / "octo_suppliers.json"
        try:
            suppliers = _json.loads(seeds_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[ENGINE] OCTO cancel: could not load supplier config: {e}")
            return False

        supplier = next(
            (s for s in suppliers if s.get("supplier_id") == platform and s.get("enabled")),
            None,
        )
        if not supplier:
            print(f"[ENGINE] OCTO cancel: no enabled supplier for '{platform}'")
            return False

        api_key = os.getenv(supplier["api_key_env"], "").strip()
        if not api_key:
            print(f"[ENGINE] OCTO cancel: API key not set ({supplier['api_key_env']})")
            return False

        base_url = supplier["base_url"].rstrip("/")
        try:
            r = _req.delete(
                f"{base_url}/bookings/{confirmation}",
                headers={
                    "Authorization":     f"Bearer {api_key}",
                    "Octo-Capabilities": "octo/pricing",
                    "Content-Type":      "application/json",
                },
                timeout=15,
            )
            if r.status_code in (200, 204, 404):
                print(f"[ENGINE] OCTO cancel succeeded (HTTP {r.status_code}): {confirmation}")
                return True
            print(f"[ENGINE] OCTO cancel failed (HTTP {r.status_code}): {r.text[:200]}")
            return False
        except Exception as e:
            print(f"[ENGINE] OCTO cancel exception: {e}")
            return False

    # ── Confidence score ──────────────────────────────────────────────────────

    def _compute_confidence(self, req: ExecutionRequest) -> float:
        """
        Return a confidence score (0.0 – 1.0) that the request can be fulfilled.

        Factors:
        - Number of matching slots available
        - How many are within the time window
        - How recently data was refreshed
        """
        available = self._available_slots()
        matching  = [s for s in available if self._score_similarity(s, req) >= 0.5]

        score = 0.0
        # Slot availability component (up to 0.6)
        n = len(matching)
        if n >= 10:   score += 0.6
        elif n >= 5:  score += 0.45
        elif n >= 2:  score += 0.3
        elif n >= 1:  score += 0.15

        # Data freshness component (up to 0.3)
        status_file = ROOT / ".tmp" / "watcher_status.json"
        if status_file.exists():
            try:
                statuses = json.loads(status_file.read_text(encoding="utf-8"))
                now = datetime.now(timezone.utc)
                freshest_age = 9999
                for plat_status in statuses.values():
                    last = plat_status.get("last_poll", "")
                    if last:
                        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        age_min = (now - dt).total_seconds() / 60
                        freshest_age = min(freshest_age, age_min)
                if freshest_age < 2:    score += 0.3
                elif freshest_age < 5:  score += 0.2
                elif freshest_age < 30: score += 0.1
            except Exception:
                score += 0.1  # data exists but status unclear
        else:
            score += 0.1  # no watcher running; pipeline data assumed ~30min old

        # Same-platform booking success component (up to 0.1) — placeholder
        score += 0.1

        return min(round(score, 2), 1.0)

    # ── Main execution method ─────────────────────────────────────────────────

    def execute(self, req: ExecutionRequest) -> ExecutionResult:
        """
        Run the full multi-path execution strategy.

        Returns ExecutionResult with full audit trail regardless of success/failure.
        """
        confidence = self._compute_confidence(req)
        log: list[AttemptRecord] = []

        # Load market_insights once per execute() call, not once per attempt
        _ins_mod = None
        try:
            _ins_spec = importlib.util.spec_from_file_location("market_insights", TOOLS_DIR / "market_insights.py")
            if _ins_spec:
                _ins_mod = importlib.util.module_from_spec(_ins_spec)
                _ins_spec.loader.exec_module(_ins_mod)
        except Exception:
            pass

        strategies = [
            ("exact",         "Original slot"),
            ("exact",         "Retry original slot"),  # same slot, transient failure retry
            ("similar",       "Similar slot (same category/city, nearby time)"),
            ("category_city", "Any slot (same category/city)"),
            ("any_platform",  "Any platform (same category/city)"),
            ("metro",         "Metro area match (broader city)"),
            ("alternatives",  "Alternative category"),
        ]

        attempt_num    = 0
        fallbacks_used = 0

        for strategy_key, strategy_label in strategies:
            if attempt_num >= self.MAX_ATTEMPTS:
                break

            # Skip alternatives if not allowed
            if strategy_key == "alternatives" and not req.allow_alternatives:
                continue

            candidates = self._find_candidates(req, strategy_key)
            if not candidates:
                continue

            # Try the top 1-2 candidates for this strategy
            top_n = 2 if strategy_key in ("category_city", "any_platform", "metro", "alternatives") else 1
            for slot in candidates[:top_n]:
                attempt_num += 1
                sid   = slot.get("slot_id", "")
                name  = slot.get("service_name", "Booking")
                plat  = slot.get("platform", "")
                price = float(slot.get("our_price") or slot.get("price") or 0)

                print(f"[ENGINE] Attempt {attempt_num}: {strategy_label} — {name} ({sid})")

                if attempt_num > 1:
                    fallbacks_used += 1

                try:
                    confirmation = self._attempt_booking(slot, req.customer)

                    # Booking succeeded — now handle payment
                    payment_ok = True
                    if req.payment_method == "wallet" and req.wallet_id:
                        payment_ok = self._charge_wallet(req.wallet_id, int(price * 100), sid)
                    elif req.payment_method == "stripe_pi" and req.payment_intent_id:
                        payment_ok = self._capture_stripe(req.payment_intent_id)
                    # stripe_checkout: capture already handled by Stripe webhook

                    if not payment_ok:
                        # Payment failed after booking succeeded — cancel the supplier booking
                        # so we don't leave an unpaid confirmed reservation on their system.
                        print(f"[ENGINE] Payment failed — rolling back OCTO booking {confirmation} on {plat}")
                        self._cancel_octo(plat, str(confirmation or ""))
                        # Also cancel any Stripe hold that may still be open
                        if req.payment_method == "stripe_pi" and req.payment_intent_id:
                            self._cancel_stripe(req.payment_intent_id)
                        log.append(AttemptRecord(
                            attempt=attempt_num, strategy=strategy_label,
                            slot_id=sid, service_name=name, platform=plat, price=price,
                            outcome="payment_failed", error="Payment capture failed after booking; OCTO booking rolled back",
                        ))
                        continue

                    # Mark booked — write atomically via a temp file to avoid partial writes
                    self.booked_ids.add(sid)
                    booked_file = ROOT / ".tmp" / "booked_slots.json"
                    try:
                        existing = json.loads(booked_file.read_text(encoding="utf-8")) if booked_file.exists() else []
                        existing.append(sid)
                        tmp = booked_file.with_suffix(".tmp")
                        tmp.write_text(json.dumps(existing), encoding="utf-8")
                        tmp.replace(booked_file)  # atomic rename
                    except Exception:
                        pass

                    log.append(AttemptRecord(
                        attempt=attempt_num, strategy=strategy_label,
                        slot_id=sid, service_name=name, platform=plat, price=price,
                        outcome="success", confirmation=str(confirmation or ""),
                    ))

                    savings = round(float(slot.get("original_price") or price) - price, 2)

                    # Record to market insights (module loaded once above)
                    try:
                        if _ins_mod:
                            _ins_mod.record_booking_outcome(
                                platform=plat,
                                category=slot.get("category", ""),
                                city=slot.get("location_city", ""),
                                success=True,
                                attempts=attempt_num,
                                price=price,
                                hours_before_start=slot.get("hours_until_start") or 0,
                                slot_id=sid,
                            )
                            _ins_mod.record_slot_booked(sid, plat, slot.get("category", ""), slot.get("location_city", ""), price)
                    except Exception:
                        pass

                    return ExecutionResult(
                        success=True,
                        status="booked",
                        confirmation=str(confirmation or ""),
                        slot_id=sid,
                        service_name=name,
                        platform=plat,
                        price_charged=price,
                        attempts=attempt_num,
                        fallbacks_used=fallbacks_used,
                        savings_vs_market=max(savings, 0.0),
                        confidence_score=confidence,
                        attempt_log=log,
                    )

                except Exception as e:
                    err = str(e)[:300]
                    print(f"[ENGINE] Attempt {attempt_num} failed: {err}")
                    log.append(AttemptRecord(
                        attempt=attempt_num, strategy=strategy_label,
                        slot_id=sid, service_name=name, platform=plat, price=price,
                        outcome="booking_failed", error=err,
                    ))
                    # Record failure to market insights (module loaded once above)
                    try:
                        if _ins_mod:
                            _ins_mod.record_booking_outcome(
                                platform=plat, category=slot.get("category", ""),
                                city=slot.get("location_city", ""), success=False,
                                attempts=attempt_num, price=price,
                                hours_before_start=slot.get("hours_until_start") or 0,
                                slot_id=sid, error_type=type(e).__name__,
                            )
                    except Exception:
                        pass

        # All attempts exhausted
        if req.payment_method == "stripe_pi" and req.payment_intent_id:
            self._cancel_stripe(req.payment_intent_id)

        return ExecutionResult(
            success=False,
            status="failed" if log else "no_slots",
            attempts=attempt_num,
            fallbacks_used=fallbacks_used,
            confidence_score=confidence,
            attempt_log=log,
            error=f"All {attempt_num} booking attempt(s) failed." if log else "No matching slots found.",
        )
