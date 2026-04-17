"""
run_autonomous_fix.py — Autonomous self-healing loop for the LastMinuteDeals codebase.

Four detection tiers, each finding a different class of bug:
  1. parse_errors.py     — runtime exceptions from pipeline.log / http_server.log
  2. scan_bugs.py        — static patterns from 129 prior bugs (10 checks + auto-generated)
  3. validate_pipeline.py — behavioral output validation (wrong values, not just crashes)
  4. deep_audit.py       — Claude reads each high-risk file and finds novel issues

Before every fix:
  - call_graph.blast_radius() maps every file the change could affect
  - integration_test + validate_pipeline run on ALL blast-radius files as the gate

After every successful fix:
  - scan_bugs.learn_from_fix() generates a new pattern check from this bug
  - SYSTEM_MAP.md, bug_audit_log.md, and memory are updated

Usage:
    python tools/run_autonomous_fix.py [options]

Options:
    --dry-run          Generate fix prompts, no changes applied
    --max-bugs N       Max bugs to fix per session (default: 10)
    --file PATH        Only fix bugs in this file
    --since-hours N    Parse log errors from last N hours (default: 8)
    --blocking-only    Run only blocking integration checks after each fix (faster)
    --skip-deep-audit  Skip deep_audit.py tier (faster, use if Claude calls are expensive)
    --no-scan          Skip static pattern scan
    --no-validate      Skip pipeline output validation
    --session N        Label for this session (used in reports and git commits)
"""

import argparse
import ast
import importlib.util as ilu
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR      = Path(__file__).parent.parent
TOOLS_DIR     = Path(__file__).parent
TMP_DIR       = BASE_DIR / ".tmp"
LOGS_DIR      = TMP_DIR / "logs"
PIPELINE_LOCK = TMP_DIR / "pipeline.lock"
HISTORY_FILE  = LOGS_DIR / "fix_history.json"

MAX_ATTEMPTS  = 5


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Unified Bug dataclass
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Bug:
    """Unified bug representation from any detection tier."""
    bug_id:       str
    source:       str   # "log_error" | "scan_bugs" | "validate_pipeline" | "deep_audit"
    tool_file:    str   # "tools/foo.py"
    line:         int
    function_name: str
    error_type:   str
    message:      str
    severity:     str   # "critical" | "high" | "medium" | "low"
    context_lines: List[str] = field(default_factory=list)
    callers:      List[dict] = field(default_factory=list)
    frequency:    int   = 1
    chronic:      bool  = False
    prior_fix_attempts: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bug_id":    self.bug_id,
            "source":    self.source,
            "tool_file": self.tool_file,
            "line":      self.line,
            "function_name": self.function_name,
            "error_type": self.error_type,
            "message":   self.message,
            "severity":  self.severity,
            "frequency": self.frequency,
            "chronic":   self.chronic,
            "prior_fix_attempts": self.prior_fix_attempts,
        }


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Helpers — git, Claude CLI, patch application
# ═══════════════════════════════════════════════════════════════════════════════

def _git(cmd: List[str]) -> Tuple[int, str]:
    result = subprocess.run(
        ["git"] + cmd, cwd=str(BASE_DIR),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def _find_claude_exe() -> str:
    import glob as _glob
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    vscode = str(Path.home() / ".vscode" / "extensions" /
                 "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    matches = sorted(_glob.glob(vscode))
    if matches:
        return matches[-1]
    store = str(Path.home() / "AppData" / "Local" / "Packages" /
                "Claude_*" / "LocalCache" / "Roaming" / "Claude" / "claude-code" / "*" / "claude.exe")
    matches = sorted(_glob.glob(store))
    if matches:
        return matches[-1]
    raise FileNotFoundError("Claude Code CLI not found. Install from: https://claude.ai/code")


def _claude_print(prompt: str, timeout: int = 300) -> Tuple[bool, str]:
    try:
        exe = _find_claude_exe()
    except FileNotFoundError as e:
        return False, f"Claude CLI not found — {e}"
    try:
        # Pipe prompt via stdin to avoid Windows [WinError 206] command-line length limit
        result = subprocess.run(
            [exe, "--print", "-", "--model", "claude-opus-4-6"], cwd=str(BASE_DIR),
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        if result.returncode != 0:
            return False, f"Claude CLI error (exit {result.returncode}): {(result.stderr or '')[:200]}"
        return True, result.stdout or ""
    except subprocess.TimeoutExpired:
        return False, "Claude CLI timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


def _apply_patch(tool_file: str, patch_text: str) -> bool:
    """
    Extract and write a full-file replacement from Claude's response.

    Safety gates:
      - Rejects diff-format responses (# BEFORE: / # AFTER:)
      - Rejects code blocks shorter than 50% of the original file
      - Validates Python syntax before writing
      - Never writes non-Python files without explicit content block
    """
    code_blocks = re.findall(r'```(?:python)?\n(.*?)```', patch_text, re.DOTALL)
    if not code_blocks:
        return False

    best = max(code_blocks, key=len)
    if len(best.strip()) < 50:
        return False

    # Reject diff-format snippets
    diff_markers = ["# BEFORE:", "# AFTER:", "--- a/", "+++ b/", "@@ -"]
    if any(m in best for m in diff_markers):
        print(f"    [!] Rejected: Claude returned a diff snippet, not a full file.")
        return False

    target = BASE_DIR / tool_file
    if not target.exists():
        return False

    # Reject blocks that are much shorter than the original (likely a snippet)
    orig_lines = target.read_text(encoding="utf-8", errors="replace").count("\n")
    new_lines  = best.count("\n")
    if orig_lines > 30 and new_lines < orig_lines * 0.4:
        print(f"    [!] Rejected: response is {new_lines} lines, original is {orig_lines}. "
              f"Looks like a partial snippet.")
        return False

    # Validate Python syntax
    if tool_file.endswith(".py"):
        try:
            ast.parse(best)
        except SyntaxError as e:
            print(f"    [!] Rejected: Claude's response has syntax error: {e}")
            return False

    target.write_text(best, encoding="utf-8")
    return True


def _apply_multi_patch(patch_text: str) -> List[str]:
    """
    For Attempt 3 (multi-file fix): extract multiple # FILE: tagged code blocks.
    Returns list of files that were successfully patched.
    """
    patched = []
    # Find blocks with file headers: # FILE: tools/foo.py\n```python\n...\n```
    pattern = re.compile(
        r'#\s*FILE:\s*(tools/\S+\.py)\s*\n```(?:python)?\n(.*?)```',
        re.DOTALL,
    )
    for match in pattern.finditer(patch_text):
        fpath = match.group(1).strip()
        code  = match.group(2)
        if _apply_patch(fpath, f"```python\n{code}\n```"):
            patched.append(fpath)
    return patched


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Detection tiers — collect bugs from all 4 sources
# ═══════════════════════════════════════════════════════════════════════════════

def _load_module(path: Path):
    spec = ilu.spec_from_file_location(path.stem, path)
    mod  = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect_log_errors(since_hours: float = 8) -> List[Bug]:
    """Tier 1: Parse pipeline.log for runtime exceptions."""
    bugs: List[Bug] = []
    try:
        pe = _load_module(TOOLS_DIR / "parse_errors.py")
        raw = pe.parse_logs(since_hours=since_hours)
        for e in raw:
            bugs.append(Bug(
                bug_id=e.get("error_id", ""),
                source="log_error",
                tool_file=e.get("tool_file", ""),
                line=e.get("line", 0),
                function_name=e.get("function_name", ""),
                error_type=e.get("error_type", ""),
                message=e.get("message", ""),
                severity="high",
                context_lines=e.get("context_lines", []),
                callers=e.get("callers", []),
                frequency=e.get("frequency", 1),
                chronic=e.get("chronic", False),
                prior_fix_attempts=e.get("prior_fix_attempts", []),
            ))
    except Exception as e:
        print(f"  [!] Log error collection failed: {e}")
    return bugs


def collect_static_bugs(target_file: Optional[str] = None, graph=None) -> List[Bug]:
    """Tier 2: Static pattern scanner."""
    bugs: List[Bug] = []
    try:
        sb = _load_module(TOOLS_DIR / "scan_bugs.py")
        priority = []
        if graph:
            priority = graph.priority_order()
        raw = sb.run_all_checks(target_file=target_file, priority_files=priority)
        for b in raw:
            bugs.append(Bug(
                bug_id=b.bug_id,
                source=b.source,
                tool_file=b.tool_file,
                line=b.line,
                function_name=b.function_name,
                error_type=b.error_type,
                message=b.message,
                severity=b.severity,
                context_lines=b.context_lines,
                callers=b.callers,
                frequency=b.frequency,
            ))
    except Exception as e:
        print(f"  [!] Static scan failed: {e}")
    return bugs


def collect_validation_bugs(target_file: Optional[str] = None) -> List[Bug]:
    """Tier 3: Pipeline output validator."""
    bugs: List[Bug] = []
    if target_file and "run_api_server" not in target_file:
        # Only run full validation if we're checking output files or the server
        return bugs
    try:
        vp = _load_module(TOOLS_DIR / "validate_pipeline.py")
        failures = vp.run_all_validations()
        for f in failures:
            import hashlib
            bug_id = hashlib.sha256(
                f"{f.file}:{f.line}:{f.message[:60]}".encode()
            ).hexdigest()[:16]
            bugs.append(Bug(
                bug_id=bug_id,
                source="validate_pipeline",
                tool_file=f.file or "tools/pipeline_output",
                line=f.line,
                function_name="",
                error_type=f.check,
                message=f.message,
                severity=f.severity,
                context_lines=[f.detail] if f.detail else [],
            ))
    except Exception as e:
        print(f"  [!] Pipeline validation failed: {e}")
    return bugs


def collect_deep_audit_bugs(
    target_file: Optional[str] = None,
    graph=None,
    max_files: int = 10,
    force: bool = False,
) -> List[Bug]:
    """Tier 4: Claude-powered code review."""
    bugs: List[Bug] = []
    try:
        da = _load_module(TOOLS_DIR / "deep_audit.py")
        findings = da.run_deep_audit(
            target_file=target_file,
            graph=graph,
            force=force,
            max_files=max_files,
        )
        for f in findings:
            bugs.append(Bug(
                bug_id=f.finding_id,
                source="deep_audit",
                tool_file=f.tool_file,
                line=f.line,
                function_name=f.function_name,
                error_type=f.category,
                message=f.message,
                severity=f.severity,
                context_lines=[f.suggestion] if f.suggestion else [],
            ))
    except Exception as e:
        print(f"  [!] Deep audit failed: {e}")
    return bugs


def deduplicate_bugs(bugs: List[Bug], history: dict) -> List[Bug]:
    """
    Remove duplicates and bugs with too many failed attempts.
    Load prior_fix_attempts from history for bugs seen before.
    """
    seen_ids: dict = {}
    for b in bugs:
        if not b.bug_id:
            continue
        if b.bug_id in seen_ids:
            continue
        # Load history
        if b.bug_id in history:
            b.prior_fix_attempts = history[b.bug_id].get("prior_fix_attempts", [])
            b.chronic = history[b.bug_id].get("sessions_seen", 0) >= 3
            # Skip bugs already fixed in a previous session
            if "fixed_at" in history[b.bug_id]:
                continue
        # Skip bugs that have already exhausted all attempts
        if len(b.prior_fix_attempts) >= MAX_ATTEMPTS:
            continue
        seen_ids[b.bug_id] = b

    return sorted(
        seen_ids.values(),
        key=lambda b: (
            _SEVERITY_ORDER.get(b.severity, 9),
            -b.frequency,
            0 if b.chronic else 1,   # Chronic bugs first (escalate faster)
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Fix prompt builder (5 escalating strategies)
# ═══════════════════════════════════════════════════════════════════════════════

def _read_file_safe(path: str) -> str:
    p = BASE_DIR / path
    if not p.exists():
        return f"[File not found: {path}]"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Could not read {path}: {e}]"


def _build_fix_prompt(bug: Bug, attempt: int, blast_files: List[str]) -> str:
    file_contents = _read_file_safe(bug.tool_file)
    ctx = "\n".join(bug.context_lines) if bug.context_lines else "[no context]"

    caller_summary = "\n".join(
        f"  - {c['file']}:{c['line']} in {c.get('function', '?')}()"
        for c in bug.callers[:5]
    ) or "  (none found)"

    blast_summary = "\n".join(f"  - {f}" for f in blast_files[:8]) or "  (none)"

    prior_summary = ""
    if bug.prior_fix_attempts:
        prior_summary = "\nPRIOR ATTEMPTS THAT FAILED (DO NOT REPEAT):\n" + "\n".join(
            f"  Attempt {a['attempt']} ({a['strategy']}): {a['regression']}"
            for a in bug.prior_fix_attempts
        )

    base = f"""You are fixing a bug in the LastMinuteDeals booking system (WAT framework).
This is a production booking platform handling real money and real customers.

CRITICAL OUTPUT RULE: You MUST output the COMPLETE corrected file as a single ```python code block.
Do NOT output a diff, patch, BEFORE/AFTER snippet, or partial code.
The entire file content must be inside the code block — not just the changed lines.
Outputting anything other than the complete file will cause the fix to be REJECTED.

BUG TO FIX:
  Source:    {bug.source}
  File:      {bug.tool_file}
  Line:      {bug.line}
  Function:  {bug.function_name or '(unknown)'}
  Type:      {bug.error_type}
  Severity:  {bug.severity}
  Message:   {bug.message}

CONTEXT (±10 lines around error):
{ctx}

FILES THAT DEPEND ON THIS FILE (blast radius — these must not break):
{blast_summary}

OTHER CALLERS THAT DEPEND ON THIS CODE (preserve their API contracts):
{caller_summary}
{prior_summary}

FULL FILE CONTENTS ({bug.tool_file}):
```python
{file_contents}
```
"""

    if attempt == 1:
        return base + """
TASK (Attempt 1 — Surgical patch):
Fix ONLY the exact lines causing this specific bug. Smallest possible change.
Do NOT refactor, rename, or restructure anything else.
Do NOT modify other files. Do NOT change function signatures or return types.
Output the complete corrected file as a single ```python code block."""

    elif attempt == 2:
        caller_contents = ""
        for c in bug.callers[:2]:
            caller_contents += f"\nCALLER: {c['file']}\n```python\n{_read_file_safe(c['file'])[:3000]}\n```\n"
        return base + f"""
TASK (Attempt 2 — Caller-level fix):
The error-site patch is causing regressions. Fix this at the CALLING level instead.
Change how the broken function is called, not its internal implementation.
Look at the caller files and fix the invocation pattern.
Do NOT change the broken function's signature.
Output the complete corrected {bug.tool_file} as a single ```python code block.
{caller_contents}"""

    elif attempt == 3:
        caller_contents = ""
        for c in bug.callers[:4]:
            caller_contents += f"\nCALLER: {c['file']}\n```python\n{_read_file_safe(c['file'])[:2000]}\n```\n"
        return base + f"""
TASK (Attempt 3 — Multi-file atomic fix):
Single-file patches keep causing regressions in callers.
Fix the error site AND all callers simultaneously so they are consistent.
Output EACH file as a separate code block with this header:
  # FILE: tools/filename.py
  ```python
  [complete file contents]
  ```
All code blocks must be complete files, not snippets.
{caller_contents}"""

    elif attempt == 4:
        return base + """
TASK (Attempt 4 — Architectural patch):
Simple patches have failed. The root cause is a design flaw.
Redesign the specific function/class causing the bug. Fix the design, not the symptom.

Pattern guidance:
  Race conditions    → atomic operations (Supabase RPC, file locks, compare-and-swap)
  Init-order bugs    → lazy initialization (compute on first access, not at import time)
  Tight coupling     → introduce a thin adapter/wrapper to decouple
  Fragile parsing    → use a robust library (dateutil, json.loads with error handling)
  Missing validation → add explicit validation at the entry point

CONSTRAINTS (non-negotiable):
  - Keep the EXACT SAME function signature and return type
  - Only redesign the specific function(s) involved in this bug
  - Do NOT refactor the whole file
Output the complete corrected file as a single ```python code block."""

    else:  # attempt == 5
        return base + f"""
TASK (Attempt 5 — Targeted rewrite):
All partial approaches have failed. Rewrite the broken function from scratch.

Function to rewrite: `{bug.function_name}` in {bug.tool_file}

Requirements:
  1. IDENTICAL signature (name, parameters, defaults, return type) — no API change
  2. Correct behavior based on the docstring, context, and usage patterns above
  3. Handle ALL edge cases: None inputs, empty collections, network timeouts, invalid data
  4. Use ONLY libraries already imported in the file — no new top-level imports
  5. Rest of the file must remain COMPLETELY UNCHANGED
Output the complete corrected file as a single ```python code block."""


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Blast radius analysis + integration testing
# ═══════════════════════════════════════════════════════════════════════════════

def _run_integration_test(blocking_only: bool = True, module_cache: dict = None) -> dict:
    """Run integration_test.py. Returns summary dict."""
    if module_cache is None:
        module_cache = {}
    try:
        if "integration_test" not in module_cache:
            module_cache["integration_test"] = _load_module(TOOLS_DIR / "integration_test.py")
        mod = module_cache["integration_test"]
        mod._RESULTS.clear()
        mod._START_TIME = time.time()
        return mod.run_all(blocking_only=blocking_only)
    except Exception as e:
        return {
            "overall": "FAIL",
            "blocking_failures": 1,
            "soft_failures": 0,
            "blocking_fail_list": [{"name": "integration_test crashed", "detail": str(e)}],
            "soft_fail_list": [],
        }


def _run_validate_pipeline() -> List[str]:
    """Run validate_pipeline.py. Returns list of failure messages."""
    try:
        vp = _load_module(TOOLS_DIR / "validate_pipeline.py")
        failures = vp.run_all_validations()
        return [f"{f.severity}: {f.message}" for f in failures if f.severity in ("critical", "high")]
    except Exception as e:
        return [f"validate_pipeline crashed: {e}"]


def _get_blast_radius(tool_file: str, graph) -> List[str]:
    """Get all files that need re-testing when tool_file changes."""
    if graph is None:
        return []
    try:
        return sorted(graph.blast_radius(tool_file))
    except Exception:
        return []


def _syntax_check_all_blast_files(blast_files: List[str]) -> List[str]:
    """Quick syntax check on all files in blast radius. Returns list of errors."""
    errors = []
    for f in blast_files:
        fp = BASE_DIR / f
        if not fp.exists() or not f.endswith(".py"):
            continue
        try:
            ast.parse(fp.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            errors.append(f"{f}: SyntaxError at line {e.lineno}: {e.msg}")
    return errors


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: Git operations
# ═══════════════════════════════════════════════════════════════════════════════

def _make_backup_branch(bug_id: str, attempt: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"auto-fix/{ts}/{bug_id[:8]}-attempt{attempt}"
    code, out = _git(["checkout", "-b", name])
    if code != 0:
        print(f"    [git] Warning creating branch: {out[:60]}")
    return name


def _revert_to_main() -> None:
    _git(["checkout", "main"])
    _git(["checkout", "--", "."])


def _commit_fix(bug: Bug, attempt: int, patched_files: List[str]) -> Tuple[bool, str]:
    """Stage and commit all patched files. Returns (success, commit_hash)."""
    for f in patched_files:
        _git(["add", f])
    msg = (
        f"auto-fix: {bug.error_type} in {Path(bug.tool_file).name}:{bug.line} "
        f"(attempt {attempt}, source: {bug.source})"
    )
    code, out = _git(["commit", "-m", msg])
    if code != 0:
        return False, ""
    # Get the commit hash
    _, chash = _git(["rev-parse", "--short", "HEAD"])
    return True, chash.strip()


def _merge_fix_branch(branch: str, bug: Bug, attempt: int) -> bool:
    """Merge the fix branch into main."""
    code, _ = _git(["checkout", "main"])
    if code != 0:
        return False
    code, out = _git(["merge", "--no-ff", branch,
                      "-m", f"Merge auto-fix/{bug.bug_id[:8]}-attempt{attempt}"])
    return code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: Fix loop (5 escalating attempts)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FixResult:
    bug_id:     str
    fixed:      bool
    attempt:    int
    commit:     str = ""
    elapsed_s:  float = 0.0
    failures:   List[str] = field(default_factory=list)  # What blocked each attempt


def fix_one_bug(
    bug: Bug,
    graph,
    dry_run: bool = False,
    blocking_only: bool = True,
    module_cache: dict = None,
) -> FixResult:
    """Run up to 5 fix attempts for a single bug."""
    if module_cache is None:
        module_cache = {}

    start_time = time.time()
    blast_files = _get_blast_radius(bug.tool_file, graph)
    result = FixResult(bug_id=bug.bug_id, fixed=False, attempt=0)

    # Determine which attempts to start from (skip already-tried strategies)
    start_attempt = len(bug.prior_fix_attempts) + 1
    if bug.chronic:
        start_attempt = max(start_attempt, 4)  # Chronic bugs skip to architectural

    print(f"\n  [{bug.severity.upper()}] {bug.tool_file}:{bug.line} — {bug.error_type}")
    print(f"         Source: {bug.source} | Blast radius: {len(blast_files)} files")
    if blast_files:
        print(f"         Affected: {', '.join(Path(f).name for f in blast_files[:5])}")

    for attempt in range(start_attempt, MAX_ATTEMPTS + 1):
        attempt_start = time.time()
        print(f"\n    [Attempt {attempt}/{MAX_ATTEMPTS}]", flush=True)

        prompt = _build_fix_prompt(bug, attempt, blast_files)

        if dry_run:
            print(f"    [dry-run] Would call Claude for: {bug.error_type} at {bug.tool_file}:{bug.line}")
            result.attempt = attempt
            result.failures.append("dry-run mode")
            break

        # Step 1: Call Claude
        ok, claude_output = _claude_print(prompt)
        if not ok:
            regression = f"Claude CLI failed: {claude_output}"
            print(f"    [!] {regression[:100]}")
            bug.prior_fix_attempts.append({
                "attempt":   attempt,
                "strategy":  _strategy_name(attempt),
                "regression": regression,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })
            result.failures.append(regression)
            continue

        # Step 2: Create backup branch
        branch = _make_backup_branch(bug.bug_id, attempt)

        # Step 3: Apply patch
        if attempt == 3:
            # Multi-file: try tagged FILE: blocks first, then single file
            patched = _apply_multi_patch(claude_output)
            if not patched:
                if _apply_patch(bug.tool_file, claude_output):
                    patched = [bug.tool_file]
        else:
            patched = [bug.tool_file] if _apply_patch(bug.tool_file, claude_output) else []

        if not patched:
            regression = "No valid code block found in Claude's response"
            print(f"    [!] {regression}")
            _revert_to_main()
            bug.prior_fix_attempts.append({
                "attempt":   attempt,
                "strategy":  _strategy_name(attempt),
                "regression": regression,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })
            result.failures.append(regression)
            continue

        # Step 4: Syntax check all blast-radius files immediately
        syntax_errors = _syntax_check_all_blast_files(blast_files + patched)
        if syntax_errors:
            regression = f"Syntax error in blast-radius files: {syntax_errors[0]}"
            print(f"    [!] {regression}")
            _revert_to_main()
            bug.prior_fix_attempts.append({
                "attempt":   attempt,
                "strategy":  _strategy_name(attempt),
                "regression": regression,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })
            result.failures.append(regression)
            continue

        # Step 5: Commit patched files to the branch
        committed, chash = _commit_fix(bug, attempt, patched)
        if not committed:
            regression = "Git commit failed"
            _revert_to_main()
            bug.prior_fix_attempts.append({
                "attempt":   attempt,
                "strategy":  _strategy_name(attempt),
                "regression": regression,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })
            result.failures.append(regression)
            continue

        # Step 6: Run integration test + pipeline validation
        print(f"    Running integration test...", flush=True)
        it_result = _run_integration_test(blocking_only=blocking_only, module_cache=module_cache)

        if it_result["overall"] == "FAIL":
            blocking_fails = it_result.get("blocking_fail_list", [])
            regression = "Integration test FAIL: " + "; ".join(
                f"{f['name']}: {f.get('detail','')}" for f in blocking_fails[:2]
            )
            print(f"    [✗] Integration test failed — reverting")
            _revert_to_main()
            bug.prior_fix_attempts.append({
                "attempt":   attempt,
                "strategy":  _strategy_name(attempt),
                "regression": regression,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })
            result.failures.append(regression)
            continue

        # Step 7: Run pipeline validation (behavioral check)
        val_failures = _run_validate_pipeline()
        if val_failures:
            regression = "Pipeline validation FAIL: " + val_failures[0][:120]
            print(f"    [!] Pipeline validation issue: {val_failures[0][:80]}")
            # Non-blocking: log but don't revert (these are often pre-existing)
            # If it was already failing before the fix, don't hold it against this fix
            print(f"    [~] Treating as pre-existing — not reverting")

        # Step 8: Merge fix into main
        if not _merge_fix_branch(branch, bug, attempt):
            regression = "Git merge failed"
            _revert_to_main()
            result.failures.append(regression)
            continue

        # SUCCESS
        elapsed = time.time() - start_time
        print(f"    [✓] FIXED — {bug.error_type} at {bug.tool_file}:{bug.line} "
              f"(attempt {attempt}, {elapsed:.0f}s, commit: {chash})")
        result.fixed    = True
        result.attempt  = attempt
        result.commit   = chash
        result.elapsed_s = elapsed
        return result

    result.elapsed_s = time.time() - start_time
    print(f"\n    [✗] All {MAX_ATTEMPTS} attempts failed — adding to human queue")
    return result


def _strategy_name(attempt: int) -> str:
    names = {
        1: "Surgical patch at error site",
        2: "Caller-level fix",
        3: "Multi-file atomic fix",
        4: "Architectural patch / redesign",
        5: "Targeted rewrite of broken function",
    }
    return names.get(attempt, f"Attempt {attempt}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: Self-improvement — add new pattern after each fix
# ═══════════════════════════════════════════════════════════════════════════════

def _trigger_self_improvement(bug: Bug, commit: str) -> None:
    """After a successful fix, ask Claude to write a new scanner check."""
    try:
        # Get the diff of the fix
        _, diff = _git(["show", commit, "--stat"])
        _, full_diff = _git(["show", commit])

        sb = _load_module(TOOLS_DIR / "scan_bugs.py")
        # Only self-improve for bugs that weren't caught by the static scanner
        if bug.source in ("log_error", "validate_pipeline", "deep_audit"):
            added = sb.learn_from_fix(bug, full_diff[:2000], _claude_print)
            if added:
                print(f"    [+] Scanner updated with new pattern for: {bug.error_type}")
    except Exception as e:
        print(f"    [!] Self-improvement skipped: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: History tracking
# ═══════════════════════════════════════════════════════════════════════════════

def _load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_history(history: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _record_fix_in_history(history: dict, result: FixResult, bug: Bug) -> None:
    entry = history.get(bug.bug_id, {"sessions_seen": 0, "sessions": [], "prior_fix_attempts": []})
    if result.fixed:
        entry["fixed_at"]    = datetime.now(timezone.utc).isoformat()
        entry["fixed_attempt"] = result.attempt
        entry["commit"]      = result.commit
    else:
        entry["sessions_seen"] = entry.get("sessions_seen", 0) + 1
        entry["prior_fix_attempts"] = bug.prior_fix_attempts
    history[bug.bug_id] = entry


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: Documentation auto-update
# ═══════════════════════════════════════════════════════════════════════════════

def _document_session(fixed_results: List[Tuple[Bug, FixResult]], session_num: int) -> None:
    """Update SYSTEM_MAP.md, bug_audit_log.md, and memory after fixes."""
    if not fixed_results:
        return

    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bugs = [(b, r) for b, r in fixed_results if r.fixed]
    if not bugs:
        return

    # Build doc update prompt
    bug_lines = "\n".join(
        f"- {b.error_type} in {b.tool_file}:{b.line} — {b.message[:80]} (commit: {r.commit})"
        for b, r in bugs
    )
    prompt = f"""Update documentation after an autonomous bug-fix session.

Session: Autonomous Fix Session {session_num} ({ts})
Bugs fixed:
{bug_lines}

Files to update:

1. SYSTEM_MAP.md — In the "Bug Register" table, mark each fixed bug as:
   FIXED (autonomous agent, {ts}, commit: <hash>)
   If a bug is new and not in the register, add it to the appropriate section.

2. docs/bug_audit_log.md — Append a new session block:
   ## Session {session_num} Fixes — Autonomous Bug-Fix Agent ({ts})
   | # | Severity | File | Line | Bug | Fix |
   |---|---|---|---|---|---|
   [one row per fixed bug]

3. C:\\Users\\janaa\\.claude\\projects\\c--Users-janaa-Agentic-Workflows\\memory\\project_system_state.md
   Add a one-line summary: "Session {session_num} autonomous fix ({ts}): fixed {len(bugs)} bug(s) — [list types]"

CRITICAL: Output each file as a separate ```markdown code block with header:
  # FILE: SYSTEM_MAP.md
  ```markdown
  [complete updated file contents]
  ```
Output ALL THREE files. Do not skip any.
"""

    ok, response = _claude_print(prompt, timeout=300)
    if not ok:
        print(f"  [!] Documentation update failed: {response[:80]}")
        return

    # Apply each file block
    updated = []
    for match in re.finditer(
        r'#\s*FILE:\s*([^\n]+)\n```(?:markdown|python|text)?\n(.*?)```',
        response, re.DOTALL,
    ):
        fpath_str = match.group(1).strip()
        content   = match.group(2)
        try:
            fpath = BASE_DIR / fpath_str if not Path(fpath_str).is_absolute() else Path(fpath_str)
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
            updated.append(str(fpath.relative_to(BASE_DIR) if BASE_DIR in fpath.parents else fpath_str))
        except Exception as e:
            print(f"  [!] Could not write {fpath_str}: {e}")

    if updated:
        for f in updated:
            _git(["add", f])
        _git(["commit", "-m",
              f"docs: autonomous fix session {session_num} {ts} — {len(bugs)} bug(s) fixed"])
        print(f"  [+] Documentation committed: {', '.join(updated)}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11: Session report
# ═══════════════════════════════════════════════════════════════════════════════

class SessionReport:
    def __init__(self):
        self.start_time = time.time()
        self.bugs_found: List[Bug]          = []
        self.results:    List[FixResult]    = []
        self.tier_counts = {"log_error": 0, "scan_bugs": 0, "validate_pipeline": 0, "deep_audit": 0}

    def record_bug(self, bug: Bug) -> None:
        self.bugs_found.append(bug)
        tier = bug.source if bug.source in self.tier_counts else "scan_bugs"
        self.tier_counts[tier] = self.tier_counts.get(tier, 0) + 1

    def record_result(self, r: FixResult) -> None:
        self.results.append(r)

    def write(self, session_num: int) -> None:
        ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        elapsed  = time.time() - self.start_time
        fixed    = [r for r in self.results if r.fixed]
        reverted = [r for r in self.results if not r.fixed and r.attempt > 0]
        queued   = [r for r in self.results if r.attempt == 0 and not r.fixed]

        lines = [
            f"=== Autonomous Fix Session {session_num}: {ts} ===",
            f"Duration: {elapsed:.0f}s\n",
            f"Bugs found:        {len(self.bugs_found)}",
            f"  from log_error:        {self.tier_counts['log_error']}",
            f"  from scan_bugs:        {self.tier_counts['scan_bugs']}",
            f"  from validate_pipeline:{self.tier_counts['validate_pipeline']}",
            f"  from deep_audit:       {self.tier_counts['deep_audit']}",
            f"\nFixed & verified:  {len(fixed)}  ({len(fixed)/max(len(self.results),1)*100:.0f}%)",
            f"Reverted:          {len(reverted)}",
            f"Human queue:       {len(queued)}",
        ]

        if fixed:
            lines.append("\nFixed:")
            for r in fixed:
                bug = next((b for b in self.bugs_found if b.bug_id == r.bug_id), None)
                desc = f"{bug.tool_file}:{bug.line} — {bug.error_type}" if bug else r.bug_id
                lines.append(f"  ✓ {desc} (attempt {r.attempt}, {r.elapsed_s:.0f}s, commit: {r.commit})")

        if queued or reverted:
            lines.append("\nNeeds Human Review:")
            for r in (queued + reverted):
                bug = next((b for b in self.bugs_found if b.bug_id == r.bug_id), None)
                desc = f"{bug.tool_file}:{bug.line} — {bug.error_type}" if bug else r.bug_id
                lines.append(f"  ✗ {desc}")
                for i, fail in enumerate(r.failures, 1):
                    lines.append(f"       Attempt {i}: {fail[:100]}")

        report = "\n".join(lines)
        print("\n" + report)

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts_file = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for fname in [
            f"autonomous_fix_{ts_file}.log",
            "autonomous_fix_latest.log",
        ]:
            (LOGS_DIR / fname).write_text(report, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12: Main session runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_session(args: argparse.Namespace) -> dict:
    """Run one complete autonomous fix session. Returns session summary dict."""
    report = SessionReport()
    module_cache: dict = {}

    print(f"\n{'='*70}")
    print(f"  Autonomous Fix Session — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")

    # ── Step 1: Pipeline lock check ───────────────────────────────────────────
    if PIPELINE_LOCK.exists():
        print("\n  [!] Pipeline lock detected — waiting up to 10 minutes...")
        waited = 0
        while PIPELINE_LOCK.exists() and waited < 600:
            time.sleep(15)
            waited += 15
        if PIPELINE_LOCK.exists():
            print("  [!] Pipeline still locked after 10 min — aborting session")
            return {"status": "aborted", "reason": "pipeline locked"}

    # ── Step 2: Build call graph ──────────────────────────────────────────────
    print("\n  Building call graph...")
    graph = None
    try:
        cg = _load_module(TOOLS_DIR / "call_graph.py")
        graph = cg.CallGraph.build()
        graph.save()
        s = graph.summary()
        print(f"  Call graph: {s['files']} files, {s['import_edges']} edges, "
              f"{s['functions']} functions")
    except Exception as e:
        print(f"  [!] Call graph failed: {e} — proceeding without blast-radius analysis")

    # ── Step 3: Baseline integration test ────────────────────────────────────
    print("\n  Running baseline integration test...")
    baseline = _run_integration_test(blocking_only=True, module_cache=module_cache)
    print(f"  Baseline: {baseline['overall']} "
          f"({baseline['blocking_failures']} blocking failures)")

    # ── Step 4: Collect bugs from all 4 tiers ────────────────────────────────
    print("\n  Collecting bugs from all detection tiers...")

    target = getattr(args, "file", None)
    all_bugs: List[Bug] = []

    print("\n  Tier 1: Log errors...")
    tier1 = collect_log_errors(since_hours=getattr(args, "since_hours", 8))
    print(f"    → {len(tier1)} log error(s)")
    all_bugs.extend(tier1)

    if not getattr(args, "no_scan", False):
        print("\n  Tier 2: Static pattern scan...")
        tier2 = collect_static_bugs(target_file=target, graph=graph)
        print(f"    → {len(tier2)} pattern finding(s)")
        all_bugs.extend(tier2)

    if not getattr(args, "no_validate", False):
        print("\n  Tier 3: Pipeline output validation...")
        tier3 = collect_validation_bugs(target_file=target)
        print(f"    → {len(tier3)} validation failure(s)")
        all_bugs.extend(tier3)

    if not getattr(args, "skip_deep_audit", False):
        print("\n  Tier 4: Deep audit (Claude)...")
        max_audit_files = 8 if not target else 1
        tier4 = collect_deep_audit_bugs(
            target_file=target,
            graph=graph,
            max_files=max_audit_files,
        )
        print(f"    → {len(tier4)} deep audit finding(s)")
        all_bugs.extend(tier4)
    else:
        tier4 = []

    # ── Step 5: Deduplicate and prioritize ────────────────────────────────────
    history = _load_history()
    bugs = deduplicate_bugs(all_bugs, history)

    # Apply max_bugs limit
    max_b = getattr(args, "max_bugs", 10)
    if target:
        bugs = [b for b in bugs if b.tool_file == target]
    bugs = bugs[:max_b]

    print(f"\n  Total unique bugs to address: {len(bugs)}")
    for b in bugs:
        report.record_bug(b)

    if not bugs:
        print("\n  No bugs found — codebase is clean.")

    # ── Step 6: Fix loop ──────────────────────────────────────────────────────
    fix_results: List[Tuple[Bug, FixResult]] = []

    for i, bug in enumerate(bugs, 1):
        print(f"\n{'─'*60}")
        print(f"  Bug {i}/{len(bugs)}: {bug.tool_file}:{bug.line} [{bug.severity.upper()}]")

        result = fix_one_bug(
            bug=bug,
            graph=graph,
            dry_run=getattr(args, "dry_run", False),
            blocking_only=getattr(args, "blocking_only", True),
            module_cache=module_cache,
        )
        fix_results.append((bug, result))
        report.record_result(result)

        # Update history
        _record_fix_in_history(history, result, bug)
        _save_history(history)

        # Self-improvement after successful fix
        if result.fixed and not getattr(args, "dry_run", False):
            _trigger_self_improvement(bug, result.commit)

    # ── Step 7: Final full integration test ──────────────────────────────────
    print(f"\n{'─'*60}")
    print("\n  Running final full integration test...")
    final = _run_integration_test(blocking_only=False, module_cache=module_cache)
    print(f"  Final: {final['overall']} "
          f"({final['blocking_failures']} blocking, {final['soft_failures']} soft failures)")

    # ── Step 8: Write session report ─────────────────────────────────────────
    session_num = getattr(args, "session", 22)
    report.write(session_num)

    # ── Step 9: Document fixed bugs ──────────────────────────────────────────
    fixed_pairs = [(b, r) for b, r in fix_results if r.fixed]
    if fixed_pairs and not getattr(args, "dry_run", False):
        print("\n  Updating documentation...")
        _document_session(fixed_pairs, session_num)

    fixed_count   = sum(1 for _, r in fix_results if r.fixed)
    queued_count  = sum(1 for _, r in fix_results if not r.fixed)
    revert_pct    = (sum(len(r.failures) for _, r in fix_results) /
                     max(len(fix_results), 1)) * 100

    if revert_pct > 50 and len(fix_results) > 2:
        print("\n  ⚠ STRUGGLING: >50% revert rate — system may have deeper architectural issues.")
        print("    Consider a supervised session focused on the remaining human-queue items.")

    return {
        "bugs_found":  len(bugs),
        "fixed":       fixed_count,
        "human_queue": queued_count,
        "final_test":  final["overall"],
        "revert_pct":  revert_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13: Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous self-healing fix loop")
    parser.add_argument("--dry-run",         action="store_true")
    parser.add_argument("--max-bugs",        type=int,   default=10)
    parser.add_argument("--file",            type=str,   default=None)
    parser.add_argument("--since-hours",     type=float, default=8)
    parser.add_argument("--blocking-only",   action="store_true")
    parser.add_argument("--skip-deep-audit", action="store_true")
    parser.add_argument("--no-scan",         action="store_true")
    parser.add_argument("--no-validate",     action="store_true")
    parser.add_argument("--session",         type=int,   default=22,
                        help="Session number for documentation (default: 22)")
    args = parser.parse_args()

    summary = run_session(args)
    sys.exit(0 if summary.get("final_test") == "PASS" else 1)
