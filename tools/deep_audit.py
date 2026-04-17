"""
deep_audit.py — Claude-powered systematic code review for the WAT framework.

For each file in tools/ (ordered by coupling risk from call_graph), calls
Claude CLI with the full file content plus codebase context. Claude looks for:
  - Bugs not caught by the static scanner (novel issues)
  - Logic errors, wrong assumptions, missing edge cases
  - Inconsistencies between what the code does vs what SYSTEM_MAP.md says it should
  - Cross-file contract violations (wrong function signatures, return type mismatches)

Results are cached by file hash so unchanged files are not re-audited.

Usage:
    python tools/deep_audit.py [--file tools/foo.py] [--force] [--json]
    or imported:
        from deep_audit import run_deep_audit, AuditFinding
"""

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

BASE_DIR  = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / ".tmp" / "deep_audit_cache.json"
LOGS_DIR   = BASE_DIR / ".tmp" / "logs"

sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class AuditFinding:
    """A single finding from the deep code review."""
    finding_id:   str
    tool_file:    str
    line:         int
    function_name: str
    severity:     str   # "critical" | "high" | "medium" | "low"
    category:     str   # e.g. "logic_error" | "missing_edge_case" | "api_contract" | etc.
    message:      str
    suggestion:   str = ""
    confidence:   str = "medium"  # "high" | "medium" | "low"
    source:       str = "deep_audit"

    def to_dict(self) -> dict:
        return asdict(self)


def _make_finding_id(tool_file: str, line: int, msg: str) -> str:
    raw = f"{tool_file}:{line}:{msg[:60]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Cache management ──────────────────────────────────────────────────────────

def _file_hash(filepath: Path) -> str:
    """MD5 hash of file content — used to skip unchanged files."""
    try:
        return hashlib.md5(filepath.read_bytes()).hexdigest()
    except Exception:
        return ""


def _load_cache() -> dict:
    """Load audit cache: {file_hash: [findings]}"""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ── Claude CLI helpers ────────────────────────────────────────────────────────

def _find_claude_exe() -> str:
    import shutil as _shutil
    import glob as _glob
    found = _shutil.which("claude") or _shutil.which("claude.exe")
    if found:
        return found
    vscode_pattern = str(Path.home() / ".vscode" / "extensions" /
                         "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    matches = sorted(_glob.glob(vscode_pattern))
    if matches:
        return matches[-1]
    store_pattern = str(Path.home() / "AppData" / "Local" / "Packages" /
                        "Claude_*" / "LocalCache" / "Roaming" / "Claude" /
                        "claude-code" / "*" / "claude.exe")
    matches = sorted(_glob.glob(store_pattern))
    if matches:
        return matches[-1]
    raise FileNotFoundError("Claude Code CLI not found.")


def _claude_print(prompt: str, timeout: int = 300) -> tuple:
    """Call Claude CLI. Returns (success: bool, output: str)."""
    try:
        exe = _find_claude_exe()
    except FileNotFoundError as e:
        return False, str(e)
    try:
        # Pipe prompt via stdin to avoid Windows [WinError 206] command-line length limit
        result = subprocess.run(
            [exe, "--print", "-", "--model", "claude-opus-4-6"],
            cwd=str(BASE_DIR),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            return False, f"Claude CLI exit {result.returncode}: {(result.stderr or '')[:200]}"
        return True, result.stdout or ""
    except subprocess.TimeoutExpired:
        return False, "Claude CLI timed out"
    except Exception as e:
        return False, str(e)


# ── Context builders ──────────────────────────────────────────────────────────

def _load_system_map_excerpt() -> str:
    """Load the relevant parts of SYSTEM_MAP.md (architecture + bug register)."""
    path = BASE_DIR / "SYSTEM_MAP.md"
    if not path.exists():
        return "[SYSTEM_MAP.md not found]"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        # Return first 3000 chars (architecture overview) + last 2000 chars (bug register)
        if len(content) > 6000:
            return content[:3000] + "\n\n[... truncated ...]\n\n" + content[-2000:]
        return content
    except Exception:
        return "[Could not read SYSTEM_MAP.md]"


def _load_recent_bugs() -> str:
    """Load last 30 fixed bugs from bug_audit_log.md for context."""
    path = BASE_DIR / "docs" / "bug_audit_log.md"
    if not path.exists():
        return "[bug_audit_log.md not found]"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        # Take the last portion of the audit log (most recent bugs)
        return content[-3000:] if len(content) > 3000 else content
    except Exception:
        return ""


def _load_call_context(tool_file: str, graph) -> str:
    """Build call graph context for this file: what calls it, what it calls."""
    if graph is None:
        return ""
    try:
        blast   = graph.blast_radius(tool_file)
        deps    = graph.dependencies(tool_file)
        risk    = graph.risk_level(tool_file)
        callers = graph.callers_of(Path(tool_file).stem)

        lines = [f"COUPLING RISK: {risk}"]
        if deps:
            lines.append(f"This file imports: {', '.join(sorted(deps))}")
        if blast:
            lines.append(f"Files that import THIS file ({len(blast)} total): {', '.join(sorted(blast)[:8])}")
        if callers:
            lines.append(f"Functions from this file are called by: {', '.join(sorted(callers)[:8])}")
        return "\n".join(lines)
    except Exception:
        return ""


# ── Prompt building ───────────────────────────────────────────────────────────

_KNOWN_PATTERNS = """
PATTERNS ALREADY CHECKED BY THE STATIC SCANNER (DO NOT RE-REPORT THESE):
  A1: Tuple unpack mismatch — _fulfill_booking() returns 3-tuple, caller unpacks 2
  A2: .tmp/ writes for persistent data (lost on Railway redeploy)
  A3: Octo-Capabilities header on DELETE requests (Bokun hangs)
  A4: Shared state (wallets.json, booked_slots.json) written without file lock
  A5: Cache TTL below 60 seconds
  A6: External API calls (requests, stripe, supabase) not in try/except; bare except: pass
  A7: booking_url JSON missing required OCTO keys (_type, base_url, product_id, availability_id)
  A8: Status set to 'cancelled' without sending cancellation email
  A9: .json() called on HTTP response without prior status check (HTML 502 crashes it)
  A10: Wallet debit AFTER booking call (double-spend if booking succeeds but debit fails)
""".strip()


def _build_audit_prompt(
    tool_file: str,
    source: str,
    call_context: str,
    system_map_excerpt: str,
    recent_bugs: str,
) -> str:
    return f"""You are auditing code in the LastMinuteDeals booking platform (WAT framework).
This is a production system that takes real bookings and charges real money.
Your job is to find real, specific bugs — not theoretical risks or style issues.

{_KNOWN_PATTERNS}

CALL GRAPH CONTEXT:
{call_context}

ARCHITECTURE (SYSTEM_MAP.md excerpt):
{system_map_excerpt[:2500]}

RECENT BUG HISTORY (last 30 fixes — don't re-report already-fixed bugs):
{recent_bugs[:2000]}

FILE TO AUDIT: {tool_file}
```python
{source[:8000]}
```

YOUR TASK:
Find every real bug in this file that is NOT covered by patterns A1-A10 above.

For each bug, output a JSON object (one per line, all on separate lines) in this EXACT format:
{{"line": 123, "function": "function_name", "severity": "high", "category": "logic_error", "message": "What is wrong", "suggestion": "How to fix it", "confidence": "high"}}

severity must be: critical, high, medium, or low
category must be one of: logic_error, missing_edge_case, api_contract, race_condition, data_loss, wrong_assumption, missing_validation, performance, security, other
confidence: high (certain this is a bug), medium (likely a bug), low (potential concern worth reviewing)

ONLY output JSON objects, one per line. No explanation, no preamble, no markdown.
If you find no bugs, output: {{"none": true}}
Focus on bugs that would cause real failures in production, not code style.
"""


# ── Parsing Claude's response ─────────────────────────────────────────────────

def _parse_audit_response(tool_file: str, response: str) -> List[AuditFinding]:
    """Parse Claude's JSON-per-line output into AuditFinding objects."""
    findings: List[AuditFinding] = []

    for line in response.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if obj.get("none"):
                continue
            findings.append(AuditFinding(
                finding_id=_make_finding_id(
                    tool_file, obj.get("line", 0), obj.get("message", "")
                ),
                tool_file=tool_file,
                line=int(obj.get("line", 0)),
                function_name=str(obj.get("function", "")),
                severity=obj.get("severity", "medium"),
                category=obj.get("category", "other"),
                message=str(obj.get("message", "")),
                suggestion=str(obj.get("suggestion", "")),
                confidence=obj.get("confidence", "medium"),
            ))
        except (json.JSONDecodeError, Exception):
            continue

    return findings


# ── Per-file audit ────────────────────────────────────────────────────────────

def audit_file(
    filepath: Path,
    graph=None,
    cache: dict = None,
    force: bool = False,
    system_map: str = None,
    recent_bugs: str = None,
) -> List[AuditFinding]:
    """
    Audit a single file using Claude CLI.
    Uses cache to skip unchanged files unless force=True.
    """
    rel = str(filepath.relative_to(BASE_DIR)).replace("\\", "/")
    fhash = _file_hash(filepath)

    # Check cache
    if not force and cache is not None and fhash in cache:
        cached = cache[fhash]
        if cached:
            print(f"  [{filepath.name}] {len(cached)} cached finding(s) (file unchanged)")
        return [AuditFinding(**f) for f in cached]

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return []

    # Skip files that are too small or generated
    if len(source.strip()) < 100:
        return []

    call_ctx = _load_call_context(rel, graph)
    sm       = system_map or _load_system_map_excerpt()
    rb       = recent_bugs or _load_recent_bugs()

    prompt = _build_audit_prompt(rel, source, call_ctx, sm, rb)

    print(f"  [{filepath.name}] Auditing ({len(source)} chars)...", flush=True)
    ok, response = _claude_print(prompt, timeout=240)

    if not ok:
        print(f"  [{filepath.name}] Claude failed: {response[:80]}")
        return []

    findings = _parse_audit_response(rel, response)

    # Update cache
    if cache is not None:
        cache[fhash] = [f.to_dict() for f in findings]

    if findings:
        print(f"  [{filepath.name}] {len(findings)} finding(s) found")
    else:
        print(f"  [{filepath.name}] Clean")

    return findings


# ── Full audit ────────────────────────────────────────────────────────────────

def run_deep_audit(
    target_file: Optional[str] = None,
    priority_files: Optional[List[str]] = None,
    graph=None,
    force: bool = False,
    max_files: int = 20,
) -> List[AuditFinding]:
    """
    Run deep audit on the codebase using Claude CLI.

    Args:
        target_file:    Only audit this file.
        priority_files: Audit these files first (then others up to max_files).
        graph:          CallGraph instance (for coupling context).
        force:          Re-audit even if file is cached.
        max_files:      Max files to audit per session (Claude calls are expensive).

    Returns:
        Deduplicated list of AuditFinding objects sorted by severity.
    """
    cache = _load_cache()
    system_map  = _load_system_map_excerpt()
    recent_bugs = _load_recent_bugs()

    # High-value files to always audit (high coupling = high blast radius)
    high_value = [
        "tools/run_api_server.py",
        "tools/complete_booking.py",
        "tools/execution_engine.py",
        "tools/manage_wallets.py",
        "tools/circuit_breaker.py",
        "tools/reconcile_bookings.py",
        "tools/retry_cancellations.py",
        "tools/normalize_slot.py",
        "tools/fetch_octo_slots.py",
        "tools/compute_pricing.py",
        "tools/sync_to_supabase.py",
        "tools/aggregate_slots.py",
        "tools/send_booking_email.py",
        "tools/intent_sessions.py",
    ]

    if target_file:
        files_to_audit = [BASE_DIR / target_file]
    else:
        # Order: priority first, then high-value, then everything else
        ordered_paths = []
        seen = set()

        for pf in (priority_files or []):
            p = BASE_DIR / pf
            if p.exists() and str(p) not in seen:
                ordered_paths.append(p)
                seen.add(str(p))

        for hv in high_value:
            p = BASE_DIR / hv
            if p.exists() and str(p) not in seen:
                ordered_paths.append(p)
                seen.add(str(p))

        # Fill remaining up to max_files with other tools
        for fp in sorted(TOOLS_DIR.glob("*.py")):
            if str(fp) not in seen and fp.name not in (
                "deep_audit.py", "scan_bugs.py", "scan_bugs_auto.py",
                "call_graph.py", "validate_pipeline.py", "run_autonomous_fix.py",
                "parse_errors.py", "integration_test.py",
            ):
                ordered_paths.append(fp)
                seen.add(str(fp))

        files_to_audit = ordered_paths[:max_files]

    all_findings: List[AuditFinding] = []

    for filepath in files_to_audit:
        if not filepath.exists():
            continue
        findings = audit_file(
            filepath,
            graph=graph,
            cache=cache,
            force=force,
            system_map=system_map,
            recent_bugs=recent_bugs,
        )
        all_findings.extend(findings)
        # Small delay between Claude calls to avoid rate limiting
        if len(files_to_audit) > 1:
            time.sleep(1)

    _save_cache(cache)

    # Deduplicate by finding_id
    seen_ids: dict = {}
    for f in all_findings:
        if f.finding_id not in seen_ids:
            seen_ids[f.finding_id] = f

    # Sort by severity and confidence
    sev_order  = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    conf_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        seen_ids.values(),
        key=lambda f: (sev_order.get(f.severity, 9), conf_order.get(f.confidence, 9)),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deep code audit via Claude CLI")
    parser.add_argument("--file",  help="Audit only this file")
    parser.add_argument("--force", action="store_true", help="Ignore cache, re-audit all")
    parser.add_argument("--json",  action="store_true", help="Output as JSON")
    parser.add_argument("--max",   type=int, default=10, help="Max files to audit (default 10)")
    args = parser.parse_args()

    print("Running deep audit via Claude CLI...")
    print("(Results are cached by file hash — only changed files are re-audited)")
    print()

    # Try to load call graph for context
    try:
        sys.path.insert(0, str(TOOLS_DIR))
        from call_graph import CallGraph
        graph = CallGraph.build()
    except Exception:
        graph = None

    findings = run_deep_audit(
        target_file=args.file,
        graph=graph,
        force=args.force,
        max_files=args.max,
    )

    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        if not findings:
            print("\n  No issues found (or all cached as clean).")
        else:
            print(f"\nFound {len(findings)} issue(s):\n")
            for f in findings:
                conf_flag = " [low-confidence]" if f.confidence == "low" else ""
                print(f"  [{f.severity.upper():8s}] {f.tool_file}:{f.line}{conf_flag}")
                print(f"             {f.category}: {f.message[:100]}")
                if f.suggestion:
                    print(f"             Fix: {f.suggestion[:100]}")
                print()
