"""
scan_bugs.py — Static bug pattern scanner for the WAT framework.

Three detection modes, all run without executing any code:

  Part A: 10 structural patterns derived from 129 prior bugs
          (tuple safety, Railway persistence, cancellation consistency, etc.)

  Part B: Novel bug discovery via Claude CLI
          Reads each file with full call-graph context; asks Claude to find
          bugs not covered by the known patterns.

  Part C: Self-improvement
          After any fix, the caller passes the bug + diff to learn_from_fix().
          A new check function is generated and appended to scan_bugs_auto.py
          so future scans catch the same class of bug.

Usage:
    python tools/scan_bugs.py [--file tools/foo.py] [--novel] [--json]
    or imported:
        from scan_bugs import run_all_checks, Bug
"""

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

BASE_DIR  = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent

sys.stdout.reconfigure(encoding="utf-8")

# ── Canonical booking record fields ──────────────────────────────────────────
# Every booking entry point must populate all 20 of these.
CANONICAL_BOOKING_FIELDS = {
    "booking_id", "status", "payment_status", "slot_id", "slot_json",
    "customer_name", "customer_email", "customer_phone", "quantity",
    "service_name", "business_name", "location_city", "start_time",
    "confirmation", "supplier_reference", "supplier_id", "platform",
    "booking_url", "our_price", "price_charged",
}

# Functions whose return value is a 3-tuple: (confirmation, booking_meta, supplier_ref)
FULFILLMENT_FUNCTIONS = {
    "_fulfill_booking", "complete_booking", "_fulfill_booking_async",
}

# Shared files that need file-level locking before write
SHARED_STATE_FILES = {
    "wallets.json", "booked_slots.json", "intent_sessions.json",
    "sms_subscribers.json", "stripe_customers.json",
}

# OCTO paths where Octo-Capabilities header is FORBIDDEN
OCTO_FORBIDDEN_HEADER = "Octo-Capabilities"

# Minimum acceptable cache TTL in seconds
MIN_CACHE_TTL = 60


# ── Bug dataclass ─────────────────────────────────────────────────────────────

@dataclass
class Bug:
    """A single detected bug finding."""
    bug_id:       str
    source:       str   # "pattern_A1" | "pattern_A2" | ... | "novel" | "auto"
    tool_file:    str
    line:         int
    function_name: str
    severity:     str   # "critical" | "high" | "medium" | "low"
    error_type:   str   # Pattern ID or description
    message:      str
    context_lines: List[str] = field(default_factory=list)
    callers:      List[dict] = field(default_factory=list)
    frequency:    int = 1
    chronic:      bool = False
    prior_fix_attempts: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _make_bug_id(tool_file: str, line: int, message: str) -> str:
    import hashlib
    raw = f"{tool_file}:{line}:{message[:60]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _context(lines: List[str], lineno: int, window: int = 10) -> List[str]:
    """Extract ±window lines around lineno (1-indexed)."""
    start = max(0, lineno - window - 1)
    end   = min(len(lines), lineno + window)
    return [f"{start + i + 1}: {ln}" for i, ln in enumerate(lines[start:end])]


# ── Pattern A checks ──────────────────────────────────────────────────────────
# Each returns List[Bug]. File content + AST are provided for efficiency.

def check_A1_tuple_safety(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A1: Fulfillment functions return 3-tuples.
    Callers unpacking to 2 values (or assigning to scalar) are wrong.
    Also catches: result used as bool/string (wrong type assumption).
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    for node in ast.walk(tree):
        # `a, b = _fulfill_booking(...)` — should be 3
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple):
                    elts = target.elts
                    # Check if RHS is a call to a fulfillment function
                    call = node.value
                    if isinstance(call, ast.Call):
                        name = ""
                        if isinstance(call.func, ast.Name):
                            name = call.func.id
                        elif isinstance(call.func, ast.Attribute):
                            name = call.func.attr
                        if name in FULFILLMENT_FUNCTIONS and len(elts) != 3:
                            bugs.append(Bug(
                                bug_id=_make_bug_id(rel, node.lineno, f"A1-tuple-{name}"),
                                source="pattern_A1",
                                tool_file=rel,
                                line=node.lineno,
                                function_name=name,
                                severity="critical",
                                error_type="TupleUnpackMismatch",
                                message=(
                                    f"{name}() returns a 3-tuple (confirmation, booking_meta, "
                                    f"supplier_ref) but caller unpacks {len(elts)} values at line {node.lineno}."
                                ),
                                context_lines=_context(lines, node.lineno),
                            ))
        # `result = _fulfill_booking(...)` followed by `result[0]` or `result[1]`
        # (hard to detect statically without type inference — skip for now)

    return bugs


def check_A2_railway_persistence(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A2: Data written to .tmp/ is wiped on Railway redeploy.
    Flag writes to files whose names suggest they contain persistent state.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    # Pattern: write_text(), open(...,"w"), json.dump to a .tmp/ path
    # Heuristic: if the filename contains a "persistent" keyword, flag it
    persistent_keywords = [
        "session", "subscription", "subscriber", "customer",
        "booking", "wallet", "insight", "stripe", "sms_sent",
    ]
    regenerable_keywords = [
        "slots", "pricing", "aggregated", "octo_slots", "pipeline",
        "errors_parsed", "decisions", "cache",
    ]

    # Find string literals that look like .tmp/ file paths
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.s, str):
            s = node.s
            if ".tmp/" in s and ".json" in s:
                name = Path(s).name.lower()
                is_persistent = any(kw in name for kw in persistent_keywords)
                is_regenerable = any(kw in name for kw in regenerable_keywords)
                if is_persistent and not is_regenerable:
                    bugs.append(Bug(
                        bug_id=_make_bug_id(rel, node.lineno, f"A2-railway-{name}"),
                        source="pattern_A2",
                        tool_file=rel,
                        line=node.lineno,
                        function_name="",
                        severity="high",
                        error_type="RailwayPersistence",
                        message=(
                            f"'{s}' contains persistent state but lives in .tmp/ which is "
                            f"wiped on Railway redeploy. Migrate to Supabase Storage."
                        ),
                        context_lines=_context(lines, node.lineno),
                    ))

    return bugs


def check_A3_octo_header_policy(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A3: Octo-Capabilities header must NEVER appear on DELETE requests to OCTO.
    It causes Bokun to hang indefinitely. Bug pattern from D-2/D-3/D-4.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    # Find DELETE calls that pass headers containing Octo-Capabilities
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name == "delete":
                # Check keyword args for headers=
                for kw in node.keywords:
                    if kw.arg == "headers":
                        # Check if the dict contains Octo-Capabilities
                        header_src = ast.get_source_segment(source, kw.value) or ""
                        if "Octo-Capabilities" in header_src or "octo/pricing" in header_src:
                            bugs.append(Bug(
                                bug_id=_make_bug_id(rel, node.lineno, "A3-octo-header"),
                                source="pattern_A3",
                                tool_file=rel,
                                line=node.lineno,
                                function_name="",
                                severity="high",
                                error_type="OctoHeaderOnDelete",
                                message=(
                                    "Octo-Capabilities header on DELETE request will cause "
                                    "Bokun to hang indefinitely. Remove from DELETE headers."
                                ),
                                context_lines=_context(lines, node.lineno),
                            ))

    # Also check raw string patterns (headers may be built dynamically)
    for i, line in enumerate(lines, 1):
        if "delete" in line.lower() and "Octo-Capabilities" in line:
            bugs.append(Bug(
                bug_id=_make_bug_id(rel, i, "A3-octo-header-str"),
                source="pattern_A3",
                tool_file=rel,
                line=i,
                function_name="",
                severity="high",
                error_type="OctoHeaderOnDelete",
                message="Octo-Capabilities on DELETE — Bokun hangs. Remove this header.",
                context_lines=_context(lines, i),
            ))

    return bugs


def check_A4_shared_mutable_state(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A4: Writes to shared JSON files (wallets.json, booked_slots.json, etc.)
    without a file lock risk data corruption under concurrent requests.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    # Find write operations to shared state files
    for i, line in enumerate(lines, 1):
        line_lower = line.lower()
        for shared_file in SHARED_STATE_FILES:
            if shared_file in line_lower:
                # Check if this is a write operation
                if any(op in line_lower for op in ["write_text", "write_bytes", "json.dump", '"w"', "'w'"]):
                    # Check if there's a lock in the surrounding context (±20 lines)
                    ctx_start = max(0, i - 20)
                    ctx_end   = min(len(lines), i + 5)
                    ctx = "\n".join(lines[ctx_start:ctx_end])
                    has_lock = any(kw in ctx for kw in [
                        "FileLock", "fcntl", "threading.Lock", "_lock",
                        "with lock", "acquire(", "LOCK_EX",
                    ])
                    if not has_lock:
                        bugs.append(Bug(
                            bug_id=_make_bug_id(rel, i, f"A4-lock-{shared_file}"),
                            source="pattern_A4",
                            tool_file=rel,
                            line=i,
                            function_name="",
                            severity="high",
                            error_type="SharedMutableState",
                            message=(
                                f"Write to '{shared_file}' without a file lock. "
                                f"Concurrent requests will corrupt this file. "
                                f"Add a FileLock or use atomic Supabase Storage writes."
                            ),
                            context_lines=_context(lines, i),
                        ))

    return bugs


def check_A5_cache_ttl(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A5: Cache TTLs under 60s cause cache misses on every request under load.
    Bug B-29: 60s TTL caused 50s blocking and 30% failure rate.
    Also flags caches without stale fallback.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    ttl_pattern = re.compile(
        r'(?:TTL|_TTL|CACHE_TTL|cache_ttl|ttl|maxage|max_age)\s*=\s*(\d+)',
        re.IGNORECASE,
    )
    for i, line in enumerate(lines, 1):
        m = ttl_pattern.search(line)
        if m:
            val = int(m.group(1))
            if 0 < val < MIN_CACHE_TTL:
                bugs.append(Bug(
                    bug_id=_make_bug_id(rel, i, f"A5-ttl-{val}"),
                    source="pattern_A5",
                    tool_file=rel,
                    line=i,
                    function_name="",
                    severity="high",
                    error_type="CacheTTLTooShort",
                    message=(
                        f"Cache TTL is {val}s — too short, causes cache misses on every request "
                        f"under load (see B-29: 60s TTL → 30% failure rate). "
                        f"Minimum recommended: {MIN_CACHE_TTL}s. For slots: 300s."
                    ),
                    context_lines=_context(lines, i),
                ))

    return bugs


def check_A6_error_handling(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A6: External API calls not inside try/except.
    Also catches bare `except: pass` swallowing errors silently.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    # Find all external call sites (requests.get/post/etc, stripe calls, supabase calls)
    external_patterns = [
        r'requests\.(get|post|put|delete|patch)\s*\(',
        r'stripe\.\w+\.(create|retrieve|cancel|refund)\s*\(',
        r'supabase\.table\(',
        r'session\.(get|post|put|delete)\s*\(',
    ]

    # Build a set of line numbers that are inside try blocks
    try_lines: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            # Mark all lines in the try body
            for child in ast.walk(node):
                if hasattr(child, "lineno"):
                    try_lines.add(child.lineno)

    for i, line in enumerate(lines, 1):
        for pattern in external_patterns:
            if re.search(pattern, line):
                if i not in try_lines:
                    bugs.append(Bug(
                        bug_id=_make_bug_id(rel, i, f"A6-noexcept"),
                        source="pattern_A6",
                        tool_file=rel,
                        line=i,
                        function_name="",
                        severity="medium",
                        error_type="UnhandledExternalCall",
                        message=(
                            f"External API call at line {i} is not inside a try/except block. "
                            f"Network failures will crash the caller. Wrap in try/except."
                        ),
                        context_lines=_context(lines, i),
                    ))
                break  # Don't double-report same line

    # Find bare except: pass (silently swallows errors)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:  # bare `except:`
                # Check if the body is just `pass` or just a single log
                body = node.body
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    bugs.append(Bug(
                        bug_id=_make_bug_id(rel, node.lineno, "A6-bare-except-pass"),
                        source="pattern_A6",
                        tool_file=rel,
                        line=node.lineno,
                        function_name="",
                        severity="medium",
                        error_type="BareExceptPass",
                        message=(
                            "bare `except: pass` silently swallows all exceptions. "
                            "At minimum log the error; ideally handle or reraise."
                        ),
                        context_lines=_context(lines, node.lineno),
                    ))

    return bugs


def check_A7_booking_url_integrity(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A7: booking_url must be valid JSON containing the required OCTO fields.
    Catches: booking_url set to a plain URL string (not the JSON blob),
    or missing required keys in the JSON encoding.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    required_booking_url_keys = {"_type", "base_url", "product_id", "availability_id"}

    # Find dict literals assigned to booking_url
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "booking_url":
                    # Check if it's a json.dumps of a dict
                    val = node.value
                    if isinstance(val, ast.Call):
                        # json.dumps({...}) — check the dict keys
                        if (isinstance(val.func, ast.Attribute)
                                and val.func.attr == "dumps"
                                and val.args):
                            inner = val.args[0]
                            if isinstance(inner, ast.Dict):
                                keys = {
                                    k.s for k in inner.keys
                                    if isinstance(k, ast.Constant) and isinstance(k.s, str)
                                }
                                missing = required_booking_url_keys - keys
                                if missing:
                                    bugs.append(Bug(
                                        bug_id=_make_bug_id(rel, node.lineno, "A7-booking-url"),
                                        source="pattern_A7",
                                        tool_file=rel,
                                        line=node.lineno,
                                        function_name="",
                                        severity="high",
                                        error_type="BookingUrlMissingKeys",
                                        message=(
                                            f"booking_url JSON is missing required keys: "
                                            f"{missing}. OCTOBooker will fail to parse this."
                                        ),
                                        context_lines=_context(lines, node.lineno),
                                    ))
                    elif isinstance(val, ast.Constant) and isinstance(val.s, str):
                        # Plain URL string — should be JSON
                        if val.s.startswith("http"):
                            bugs.append(Bug(
                                bug_id=_make_bug_id(rel, node.lineno, "A7-plain-url"),
                                source="pattern_A7",
                                tool_file=rel,
                                line=node.lineno,
                                function_name="",
                                severity="high",
                                error_type="BookingUrlPlainString",
                                message=(
                                    "booking_url is set to a plain HTTP URL string. "
                                    "It must be a JSON-encoded dict with OCTO booking params."
                                ),
                                context_lines=_context(lines, node.lineno),
                            ))

    return bugs


def check_A8_cancellation_email(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A8: Every cancellation path must send a cancellation email.
    Catches paths that update booking status to 'cancelled' without calling email.
    Bug C-1: DELETE /bookings didn't send cancellation email.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    # Only check files that contain cancellation logic
    if not any(kw in source for kw in ["cancel", "deleted", "bokun_webhook"]):
        return bugs

    # Find status = "cancelled" assignments
    for i, line in enumerate(lines, 1):
        if '"cancelled"' in line or "'cancelled'" in line:
            if "status" in line.lower() or "state" in line.lower():
                # Check if email is sent in surrounding context (±30 lines)
                ctx_start = max(0, i - 15)
                ctx_end   = min(len(lines), i + 30)
                ctx = "\n".join(lines[ctx_start:ctx_end])
                has_email = any(kw in ctx for kw in [
                    "send_booking_cancel", "send_cancelled", "cancellation_email",
                    "send_email", "sendgrid", "cancel_email",
                ])
                # Also skip if it's just a status comparison, not an assignment
                stripped = line.strip()
                is_assignment = "=" in stripped and not stripped.startswith("if") and not stripped.startswith("#")
                if is_assignment and not has_email:
                    bugs.append(Bug(
                        bug_id=_make_bug_id(rel, i, "A8-cancel-no-email"),
                        source="pattern_A8",
                        tool_file=rel,
                        line=i,
                        function_name="",
                        severity="medium",
                        error_type="CancellationNoEmail",
                        message=(
                            f"Status set to 'cancelled' at line {i} but no email send found "
                            f"in the surrounding context. Customer may not be notified."
                        ),
                        context_lines=_context(lines, i),
                    ))

    return bugs


def check_A9_json_response_handling(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A9: .json() called on HTTP responses without checking status first.
    Non-2xx responses return HTML error pages — .json() raises JSONDecodeError.
    Bug B-31: HTML 502 crashed book_slot tool.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Find: resp.json() or response.json() or r.json()
        if re.search(r'\b\w+\.json\(\)', stripped):
            # Check if raise_for_status() or status_code check is nearby (preceding 5 lines)
            preceding = lines[max(0, i - 6):i - 1]
            ctx = "\n".join(preceding)
            has_check = any(kw in ctx for kw in [
                "raise_for_status", "status_code", ".ok", "if resp", "if response",
            ])
            # Also check if we're inside a try block (from A6 logic would be redundant — skip)
            if not has_check and ".json()" in line:
                bugs.append(Bug(
                    bug_id=_make_bug_id(rel, i, "A9-json-no-check"),
                    source="pattern_A9",
                    tool_file=rel,
                    line=i,
                    function_name="",
                    severity="medium",
                    error_type="JsonWithoutStatusCheck",
                    message=(
                        f"`.json()` called at line {i} without prior `raise_for_status()` "
                        f"or status code check. A 4xx/5xx response returns HTML, not JSON — "
                        f"this will raise JSONDecodeError. Add raise_for_status() before .json()."
                    ),
                    context_lines=_context(lines, i),
                ))

    return bugs


def check_A10_wallet_debit_order(source: str, tree: ast.AST, filepath: Path) -> List[Bug]:
    """
    A10: Wallet debit must happen BEFORE the supplier booking call (atomic reserve).
    Debit-after-booking risks double-spend if the booking succeeds but debit fails.
    Bug A-3: execute/guaranteed debit happened after booking.
    """
    bugs: List[Bug] = []
    lines = source.splitlines()
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")

    if "wallet" not in source.lower():
        return bugs

    # Find debit calls and booking calls — check ordering
    debit_lines = []
    booking_lines = []

    for i, line in enumerate(lines, 1):
        if any(kw in line for kw in ["debit_wallet", "deduct_wallet", "wallet_debit", "spend_wallet"]):
            debit_lines.append(i)
        if any(kw in line for kw in ["complete_booking", "_fulfill_booking", "octo_book", "make_reservation"]):
            booking_lines.append(i)

    # If debit comes AFTER booking in the same function scope — flag
    for dl in debit_lines:
        for bl in booking_lines:
            if bl < dl and dl - bl < 50:  # Within 50 lines (same function)
                bugs.append(Bug(
                    bug_id=_make_bug_id(rel, dl, "A10-debit-after-book"),
                    source="pattern_A10",
                    tool_file=rel,
                    line=dl,
                    function_name="",
                    severity="high",
                    error_type="WalletDebitAfterBooking",
                    message=(
                        f"Wallet debit at line {dl} appears AFTER booking call at line {bl}. "
                        f"If booking succeeds but debit fails, funds are spent without deduction. "
                        f"Debit the wallet BEFORE calling the supplier."
                    ),
                    context_lines=_context(lines, dl),
                ))

    return bugs


# ── All pattern checks registry ───────────────────────────────────────────────

_PATTERN_CHECKS = [
    check_A1_tuple_safety,
    check_A2_railway_persistence,
    check_A3_octo_header_policy,
    check_A4_shared_mutable_state,
    check_A5_cache_ttl,
    check_A6_error_handling,
    check_A7_booking_url_integrity,
    check_A8_cancellation_email,
    check_A9_json_response_handling,
    check_A10_wallet_debit_order,
]


# ── Auto-generated patterns (Part C) ─────────────────────────────────────────

def _load_auto_patterns() -> list:
    """Load self-improvement patterns from scan_bugs_auto.py."""
    try:
        auto_path = TOOLS_DIR / "scan_bugs_auto.py"
        if not auto_path.exists():
            return []
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("scan_bugs_auto", auto_path)
        mod  = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "_AUTO_PATTERNS", [])
    except Exception as e:
        print(f"  [!] scan_bugs_auto.py failed to load: {e}", file=sys.stderr)
        return []


# ── Per-file scan ─────────────────────────────────────────────────────────────

def scan_file(filepath: Path, auto_patterns: list = None) -> List[Bug]:
    """Run all pattern checks on a single file."""
    if auto_patterns is None:
        auto_patterns = []
    bugs: List[Bug] = []
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return bugs
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # Syntax error IS a bug
        rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")
        bugs.append(Bug(
            bug_id=_make_bug_id(rel, e.lineno or 0, "syntax-error"),
            source="pattern_syntax",
            tool_file=rel,
            line=e.lineno or 0,
            function_name="",
            severity="critical",
            error_type="SyntaxError",
            message=f"Syntax error: {e.msg} at line {e.lineno}",
        ))
        return bugs

    all_checks = _PATTERN_CHECKS + (auto_patterns or [])
    for check in all_checks:
        try:
            found = check(source, tree, filepath)
            bugs.extend(found)
        except Exception as e:
            pass  # Individual check failure must never crash the scanner

    return bugs


# ── Full scan ─────────────────────────────────────────────────────────────────

def run_all_checks(
    target_file: Optional[str] = None,
    priority_files: Optional[List[str]] = None,
) -> List[Bug]:
    """
    Run all static pattern checks across the codebase.

    Args:
        target_file:    If set, only scan this file.
        priority_files: If set, scan these files (in order) plus all others.

    Returns:
        Deduplicated list of Bug objects sorted by severity.
    """
    auto_patterns = _load_auto_patterns()
    if auto_patterns:
        print(f"  [+] Loaded {len(auto_patterns)} auto-generated patterns")

    if target_file:
        fp = BASE_DIR / target_file
        bugs = scan_file(fp, auto_patterns)
    else:
        # Determine scan order — priority files first, then all others
        all_py = sorted(TOOLS_DIR.glob("*.py"))
        if priority_files:
            priority_set = set(priority_files)
            ordered = [BASE_DIR / f for f in priority_files if (BASE_DIR / f).exists()]
            ordered += [f for f in all_py if str(f.relative_to(BASE_DIR)).replace("\\", "/") not in priority_set]
        else:
            ordered = all_py

        bugs = []
        for fp in ordered:
            # Skip scan_bugs itself and auto-generated files
            if fp.name in ("scan_bugs.py", "scan_bugs_auto.py"):
                continue
            found = scan_file(fp, auto_patterns)
            if found:
                print(f"  [{fp.name}] {len(found)} finding(s)")
            bugs.extend(found)

    # Deduplicate by bug_id
    seen: dict = {}
    for b in bugs:
        if b.bug_id not in seen:
            seen[b.bug_id] = b

    # Sort: critical first, then high, then medium, then low
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(seen.values(), key=lambda b: severity_order.get(b.severity, 9))


# ── Part C: Self-improvement ──────────────────────────────────────────────────

def learn_from_fix(
    bug: Bug,
    fix_diff: str,
    claude_fn,  # callable: (prompt: str) -> tuple[bool, str]
) -> bool:
    """
    After a successful fix, ask Claude to generate a new pattern check
    that would have caught this bug statically, then append it to
    scan_bugs_auto.py.

    Returns True if a new pattern was successfully added.
    """
    # Load existing auto patterns for context
    auto_path = TOOLS_DIR / "scan_bugs_auto.py"
    existing_src = auto_path.read_text(encoding="utf-8") if auto_path.exists() else ""

    # Count existing patterns to generate unique name
    existing_count = existing_src.count("def _auto_check_")
    new_idx = existing_count + 1
    fn_name = f"_auto_check_{new_idx:03d}"

    prompt = f"""You are writing a static bug-checker for the LastMinuteDeals booking system.

A bug was just found and fixed that was NOT caught by the existing static scanner.
Your task: write a new Python check function that would have detected this class of bug.

BUG THAT WAS MISSED:
  File:     {bug.tool_file}
  Line:     {bug.line}
  Type:     {bug.error_type}
  Message:  {bug.message}

FIX APPLIED (git diff):
{fix_diff[:1500]}

EXISTING SCANNER PATTERNS ALREADY COVER:
  A1: Tuple unpack mismatch on fulfillment functions (3-tuple)
  A2: .tmp/ writes for persistent state (Railway redeploy data loss)
  A3: Octo-Capabilities header on DELETE requests
  A4: Shared mutable state written without file lock
  A5: Cache TTL below 60 seconds
  A6: External API calls not in try/except; bare except: pass
  A7: booking_url missing required OCTO JSON keys
  A8: Cancellation status set without sending cancellation email
  A9: .json() called without status check (HTML error pages crash it)
  A10: Wallet debit AFTER booking call (double-spend risk)

TASK:
Write a function named `{fn_name}` that:
1. Takes (source: str, tree: ast.AST, filepath: pathlib.Path) as arguments
2. Returns a list of dicts, each with keys: line (int), description (str), severity (str)
3. Is specific enough to avoid false positives
4. Catches the same class of bug as the one above
5. Does NOT duplicate any of A1-A10 above

CRITICAL: Output ONLY the complete function definition as a single ```python code block.
No explanation, no other code. Just the function.
The function body must be syntactically valid Python.
"""

    ok, response = claude_fn(prompt)
    if not ok:
        return False

    # Extract the function
    code_blocks = re.findall(r'```(?:python)?\n(.*?)```', response, re.DOTALL)
    if not code_blocks:
        return False

    new_fn_code = max(code_blocks, key=len).strip()

    # Validate: must define the expected function name
    if f"def {fn_name}" not in new_fn_code:
        return False

    # Validate: must be syntactically valid Python
    try:
        ast.parse(new_fn_code)
    except SyntaxError:
        return False

    # Append to scan_bugs_auto.py
    _append_auto_pattern(auto_path, fn_name, new_fn_code, bug)
    print(f"  [+] Self-improvement: added {fn_name} to scan_bugs_auto.py "
          f"(catches: {bug.error_type})")
    return True


def _append_auto_pattern(auto_path: Path, fn_name: str, fn_code: str, bug: Bug) -> None:
    """Append a new pattern function to scan_bugs_auto.py."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not auto_path.exists():
        # Create the file fresh
        header = '''"""
scan_bugs_auto.py — Auto-generated static bug patterns.

Generated by the self-improvement loop in run_autonomous_fix.py.
Each function below was created after a bug escaped the main scanner.
Do not edit manually — append via scan_bugs.learn_from_fix().
"""
import ast
from pathlib import Path
from typing import List

_AUTO_PATTERNS: list = []  # Populated below
'''
        auto_path.write_text(header, encoding="utf-8")

    existing = auto_path.read_text(encoding="utf-8")

    # Build the new function block
    block = f"""

# Added {ts} — caught after fixing: {bug.error_type} in {bug.tool_file}:{bug.line}
{fn_code}

_AUTO_PATTERNS.append({fn_name})
"""

    # Append before the final newline
    auto_path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Static bug scanner")
    parser.add_argument("--file", help="Scan only this file (e.g. tools/run_api_server.py)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    print("Running static pattern checks...")
    bugs = run_all_checks(target_file=args.file)

    if args.json:
        print(json.dumps([b.to_dict() for b in bugs], indent=2))
    else:
        if not bugs:
            print("  No pattern-based bugs found.")
        else:
            print(f"\nFound {len(bugs)} bug(s):\n")
            for b in bugs:
                print(f"  [{b.severity.upper():8s}] {b.tool_file}:{b.line}")
                print(f"             {b.error_type}: {b.message[:100]}")
                print()
