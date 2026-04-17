"""
parse_errors.py — Extract structured error records from pipeline and server logs.

Reads .tmp/logs/pipeline.log and .tmp/http_server.log, extracts errors with
their stack traces, maps each error back to its source file and line, and
identifies which other tool files call that function (so the fix loop knows
what API contracts to preserve).

Usage:
    python tools/parse_errors.py [--since-hours 4] [--log-file .tmp/logs/pipeline.log]

Output:
    .tmp/errors_parsed.json   — structured error list, sorted by frequency desc

Session history tracking:
    .tmp/logs/fix_history.json  — persists across sessions to detect chronic errors

Error record schema:
    {
        "error_id":          "sha256 hash of tool_file+error_type+line",
        "tool_file":         "tools/fetch_octo_slots.py",
        "error_type":        "HTTPError",
        "line":              200,
        "function_name":     "get_products",
        "message":           "429 Too Many Requests",
        "raw_trace":         "full stack trace string",
        "context_lines":     ["line 190...", "line 191...", ... "line 210..."],
        "callers":           [{"file": "tools/run_api_server.py", "line": 302, "function": "refresh_slots"}],
        "frequency":         3,
        "sessions_seen":     2,
        "last_seen":         "2026-04-17T14:23:11",
        "chronic":           false,
        "prior_fix_attempts": []
    }
"""

import ast
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.stdout.reconfigure(encoding="utf-8")

TOOLS_DIR    = Path(__file__).parent
BASE_DIR     = TOOLS_DIR.parent
TMP_DIR      = BASE_DIR / ".tmp"
LOGS_DIR     = TMP_DIR / "logs"
OUTPUT_FILE  = TMP_DIR / "errors_parsed.json"
HISTORY_FILE = LOGS_DIR / "fix_history.json"

# Log files to scan
LOG_FILES = [
    TMP_DIR / "logs" / "pipeline.log",
    TMP_DIR / "http_server.log",
    TMP_DIR / "logs" / "autonomous_fix_latest.log",
]

# How many sessions of the same error before it's "chronic"
CHRONIC_THRESHOLD = 3

# ── Stack trace parsing ───────────────────────────────────────────────────────

# Matches: File "/path/to/tools/something.py", line 200, in function_name
_FRAME_RE = re.compile(
    r'File "([^"]+)", line (\d+), in (\S+)'
)

# Error type at end of trace: e.g. "requests.exceptions.HTTPError: 429 Too Many Requests"
_ERROR_TYPE_RE = re.compile(
    r'^(\w[\w.]*(?:Error|Exception|Warning|Fault|Timeout|Interrupt|KeyboardInterrupt|'
    r'StopIteration|GeneratorExit|SystemExit|NotImplementedError|RuntimeError|'
    r'ValueError|TypeError|AttributeError|ImportError|OSError|IOError|'
    r'ConnectionError|TimeoutError|JSONDecodeError|KeyError|IndexError))'
    r'[:\s](.*)$',
    re.MULTILINE
)

# Timestamp at start of log line: 2026-04-17 14:23:11 or [2026-04-17 14:23:11]
_TIMESTAMP_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})'
)

# Traceback block start
_TRACEBACK_START = "Traceback (most recent call last):"
_ERROR_PREFIXES  = ("ERROR", "CRITICAL", "Exception", "Traceback")


def _extract_tracebacks(text: str) -> list[dict]:
    """Find all traceback blocks in a log file. Returns list of raw trace dicts."""
    blocks = []
    lines  = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TRACEBACK_START in line:
            # Capture timestamp from the preceding lines (look back up to 3)
            ts = None
            for back in range(1, min(4, i + 1)):
                m = _TIMESTAMP_RE.search(lines[i - back])
                if m:
                    ts = m.group(1)
                    break

            # Collect the full traceback block until we hit a blank line or next traceback
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                if _TRACEBACK_START in lines[j] and j != i:
                    break
                block_lines.append(lines[j])
                # Blank line after the error type line signals end of block
                if (j > i + 2 and lines[j].strip() == ""
                        and block_lines[-2].strip() and
                        not block_lines[-2].strip().startswith("File ")):
                    break
                j += 1

            blocks.append({"timestamp": ts, "raw": "\n".join(block_lines), "start_line": i})
            i = j
        else:
            i += 1
    return blocks


def _parse_traceback(block: dict) -> dict | None:
    """Parse a single traceback block into a structured error record."""
    raw = block["raw"]
    frames = _FRAME_RE.findall(raw)
    if not frames:
        return None

    # Find the innermost frame in the tools/ directory
    tool_frame = None
    for filepath, lineno, funcname in reversed(frames):
        p = Path(filepath)
        if "tools" in p.parts or p.name.endswith(".py"):
            # Prefer frames in our tools/ directory over stdlib/venv
            if "site-packages" not in filepath and "lib/python" not in filepath.lower():
                tool_frame = (filepath, int(lineno), funcname)
                break
    if tool_frame is None and frames:
        # Fallback: use last frame
        fp, ln, fn = frames[-1]
        tool_frame = (fp, int(ln), fn)

    filepath, lineno, funcname = tool_frame

    # Normalize to relative tools/ path
    try:
        rel = Path(filepath).relative_to(BASE_DIR)
        tool_file = str(rel).replace("\\", "/")
    except ValueError:
        tool_file = Path(filepath).name

    # Extract error type and message from the last non-empty line of the block
    error_type = "UnknownError"
    message    = ""
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        m = re.match(r'(\w[\w.]*(?:Error|Exception|Warning|Timeout|RuntimeError|ValueError|TypeError|'
                     r'AttributeError|ImportError|OSError|KeyError|IndexError|ConnectionError))[:\s](.*)', line.strip())
        if m:
            error_type = m.group(1).split(".")[-1]  # strip module prefix
            message    = m.group(2).strip()
            break
        # Plain error without matching pattern — use as message
        if not line.startswith(" ") and ":" in line:
            parts = line.split(":", 1)
            error_type = parts[0].strip().split(".")[-1]
            message    = parts[1].strip()
            break

    return {
        "timestamp":     block.get("timestamp"),
        "tool_file":     tool_file,
        "line":          lineno,
        "function_name": funcname,
        "error_type":    error_type,
        "message":       message[:200],
        "raw_trace":     raw.strip(),
        "all_frames":    [{"file": f, "line": int(l), "func": fn} for f, l, fn in frames],
    }


# ── Context line extraction ───────────────────────────────────────────────────

def _get_context_lines(tool_file: str, line: int, context: int = 20) -> list[str]:
    """Read ±context lines around the error line from the source file."""
    path = BASE_DIR / tool_file
    if not path.exists():
        return []
    try:
        src_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - context - 1)
        end   = min(len(src_lines), line + context)
        return [f"L{start+1+i}: {src_lines[start+i]}" for i in range(end - start)]
    except Exception:
        return []


# ── Caller graph (which files import/call the affected function) ──────────────

def _find_callers(tool_file: str, function_name: str) -> list[dict]:
    """
    Scan all tool .py files for imports or calls to function_name from tool_file.
    Returns list of {file, line, function} for each call site.
    """
    if not function_name or function_name in ("<module>", "main"):
        return []

    module_name = Path(tool_file).stem  # e.g. "fetch_octo_slots"
    callers = []

    for py_file in TOOLS_DIR.glob("*.py"):
        rel = str(py_file.relative_to(BASE_DIR)).replace("\\", "/")
        if rel == tool_file:
            continue
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Quick string check before full AST parse
        if function_name not in src and module_name not in src:
            continue

        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Direct function call: function_name(... or obj.function_name(
            if f"{function_name}(" in stripped:
                callers.append({"file": rel, "line": i, "function": _surrounding_func(lines, i - 1)})
            # Import: from module import function_name or import module; module.function_name
            elif f"from {module_name} import" in stripped and function_name in stripped:
                callers.append({"file": rel, "line": i, "function": "<import>"})

    return callers[:20]  # cap at 20 to avoid overwhelming the fix prompt


def _surrounding_func(lines: list[str], target_idx: int) -> str:
    """Walk upward to find the enclosing function/method name."""
    for i in range(target_idx, max(-1, target_idx - 30), -1):
        m = re.match(r'\s*(?:async\s+)?def\s+(\w+)', lines[i])
        if m:
            return m.group(1)
    return "<module>"


# ── Error deduplication and ID ────────────────────────────────────────────────

def _error_id(tool_file: str, error_type: str, line: int) -> str:
    key = f"{tool_file}:{error_type}:{line}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── Session history ───────────────────────────────────────────────────────────

def _load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_history(history: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def _update_history(errors: list[dict], history: dict) -> list[dict]:
    """
    Update session history and annotate errors with sessions_seen and chronic flag.
    Returns updated error list.
    """
    now_session = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00")
    updated = []
    for err in errors:
        eid = err["error_id"]
        rec = history.get(eid, {"sessions_seen": 0, "sessions": [], "prior_fix_attempts": []})
        # Only increment session count if we haven't seen this error in the current hour-window
        if not rec["sessions"] or rec["sessions"][-1] != now_session:
            rec["sessions"].append(now_session)
        rec["sessions_seen"] = len(set(rec["sessions"]))
        rec["last_seen"]     = err.get("last_seen") or now_session
        history[eid] = rec

        err["sessions_seen"]     = rec["sessions_seen"]
        err["chronic"]           = rec["sessions_seen"] >= CHRONIC_THRESHOLD
        err["prior_fix_attempts"] = rec.get("prior_fix_attempts", [])
        updated.append(err)

    _save_history(history)
    return updated


# ── Plain-line error patterns (no traceback — just ERROR/WARN log lines) ──────
#
# Many tools catch exceptions and log them as structured lines rather than
# letting tracebacks bubble up. Map those patterns to their source files.

_PLAIN_ERROR_PATTERNS = [
    # [SupplierName] ERROR getting products: <message>
    {
        "pattern": re.compile(r'\[.+?\] ERROR getting products: (.+)', re.IGNORECASE),
        "tool_file": "tools/fetch_octo_slots.py",
        "function_name": "get_products",
        "line": 97,
        "error_type": "ReadTimeout",
    },
    # [SupplierName] [product_id] availability error: <status>
    {
        "pattern": re.compile(r'\[.+?\] \[.+?\] availability error: (.+)', re.IGNORECASE),
        "tool_file": "tools/fetch_octo_slots.py",
        "function_name": "get_availability",
        "line": 130,
        "error_type": "HTTPError",
    },
    # WARN: Could not load pricing history from Sheets: <message>
    {
        "pattern": re.compile(r'WARN: Could not load pricing history from Sheets: (.+)', re.IGNORECASE),
        "tool_file": "tools/compute_pricing.py",
        "function_name": "load_pricing_history",
        "line": 1,
        "error_type": "OAuthError",
    },
    # ERROR: <tool-prefixed message> — generic catch-all for tool ERROR lines
    {
        "pattern": re.compile(r'^(?:\[.+?\] )?ERROR[:\s]+(.+)', re.IGNORECASE),
        "tool_file": None,   # will try to infer from context
        "function_name": "unknown",
        "line": 0,
        "error_type": "RuntimeError",
    },
]


def _extract_plain_errors(text: str, since_dt: datetime) -> list[dict]:
    """
    Parse plain ERROR/WARN log lines that have no Python traceback.
    Returns list of raw error dicts in the same schema as _parse_traceback().
    """
    results = []
    lines   = text.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Extract timestamp from the line or nearby lines
        ts = None
        m_ts = _TIMESTAMP_RE.search(line)
        if m_ts:
            ts = m_ts.group(1)
        else:
            for back in range(1, min(4, i + 1)):
                m_ts = _TIMESTAMP_RE.search(lines[i - back])
                if m_ts:
                    ts = m_ts.group(1)
                    break

        # Filter by timestamp if possible
        if ts:
            try:
                ts_dt = datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=timezone.utc)
                if ts_dt < since_dt:
                    continue
            except Exception:
                pass

        for pat in _PLAIN_ERROR_PATTERNS:
            m = pat["pattern"].search(stripped)
            if not m:
                continue
            message = m.group(1).strip()[:200]

            # Skip if this is a known non-actionable warning we can't fix via code
            if "WARN" in stripped.upper() and "invalid_grant" in message:
                # OAuth expiry is a config issue (needs re-auth), not a code bug.
                # We still want to surface it but mark it differently.
                pass  # include it — the fix loop can add a better fallback

            tool_file = pat["tool_file"]

            results.append({
                "timestamp":     ts,
                "tool_file":     tool_file or "tools/run_api_server.py",
                "line":          pat["line"],
                "function_name": pat["function_name"],
                "error_type":    pat["error_type"],
                "message":       message,
                "raw_trace":     stripped,
                "all_frames":    [],
            })
            break  # first matching pattern wins

    return results


# ── Main parse pipeline ───────────────────────────────────────────────────────

def parse_logs(since_hours: float = 8.0) -> list[dict]:
    """
    Read all log files, extract errors from the last `since_hours` hours,
    deduplicate, enrich with context lines and caller graph.
    Returns list of error dicts sorted by frequency descending.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    raw_errors: list[dict] = []

    for log_path in LOG_FILES:
        if not log_path.exists():
            continue
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Path 1: Python tracebacks
        blocks = _extract_tracebacks(text)
        for block in blocks:
            if block.get("timestamp"):
                try:
                    ts_str = block["timestamp"].replace(" ", "T")
                    ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
            parsed = _parse_traceback(block)
            if parsed:
                raw_errors.append(parsed)

        # Path 2: Plain ERROR/WARN log lines (caught exceptions logged without traceback)
        plain = _extract_plain_errors(text, cutoff)
        raw_errors.extend(plain)

    # Deduplicate by error_id and count frequency
    by_id: dict[str, dict] = {}
    for err in raw_errors:
        eid = _error_id(err["tool_file"], err["error_type"], err["line"])
        if eid not in by_id:
            by_id[eid] = {**err, "error_id": eid, "frequency": 0, "last_seen": None}
        by_id[eid]["frequency"] += 1
        # Keep the most recent timestamp
        if err.get("timestamp"):
            current = by_id[eid]["last_seen"]
            if current is None or err["timestamp"] > current:
                by_id[eid]["last_seen"] = err["timestamp"]

    errors = list(by_id.values())

    print(f"  Found {len(errors)} distinct errors across {len(raw_errors)} occurrences")

    # Enrich with context lines and caller graph
    print("  Enriching errors with context lines and call graph...")
    for err in errors:
        err["context_lines"] = _get_context_lines(err["tool_file"], err["line"])
        err["callers"]       = _find_callers(err["tool_file"], err["function_name"])

    # Load history and annotate chronic/sessions_seen
    history = _load_history()
    errors  = _update_history(errors, history)

    # Sort: chronic=False first (chronic handled separately), then by frequency desc
    errors.sort(key=lambda e: (-int(not e["chronic"]), -e["frequency"]))

    return errors


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parse pipeline logs into structured error records")
    parser.add_argument("--since-hours", type=float, default=8.0,
                        help="Only include errors from the last N hours (default: 8)")
    parser.add_argument("--log-file", type=str, default=None,
                        help="Additional log file to scan (appended to default list)")
    args = parser.parse_args()

    if args.log_file:
        LOG_FILES.append(Path(args.log_file))

    print(f"Parsing error logs (last {args.since_hours}h)...")
    errors = parse_logs(since_hours=args.since_hours)

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(errors, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  → Written to {OUTPUT_FILE} ({len(errors)} errors)")

    if errors:
        print(f"\nTop errors:")
        for e in errors[:5]:
            chronic_tag = " [CHRONIC]" if e["chronic"] else ""
            print(f"  [{e['frequency']}x] {e['tool_file']}:{e['line']} "
                  f"{e['error_type']}: {e['message'][:60]}{chronic_tag}")

    return errors


if __name__ == "__main__":
    main()
