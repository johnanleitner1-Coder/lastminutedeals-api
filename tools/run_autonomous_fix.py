"""
run_autonomous_fix.py — Autonomous self-healing loop for the LastMinuteDeals codebase.

Runs after every pipeline execution (or on demand). Reads error logs, applies targeted
fixes using Claude Code CLI, verifies the full system after each fix, and auto-reverts
any fix that introduces a regression.

Fix strategy (5 escalating attempts per error before escalating to human queue):
  1. Surgical patch at the exact error location
  2. Fix at the calling level (how the broken function is invoked)
  3. Multi-file atomic fix (error site + all callers simultaneously)
  4. Architectural patch (redesign the specific function/class to fix the root cause)
  5. Targeted rewrite (rewrite the broken function from scratch, same API contract)

Usage:
    python tools/run_autonomous_fix.py [--dry-run] [--max-errors 5] [--file tools/foo.py]

Args:
    --dry-run         Generate fix prompts but don't apply or commit anything
    --max-errors N    Max errors to address per session (default: 5)
    --file PATH       Only fix errors in this specific file
    --since-hours N   Parse errors from last N hours (default: 8)
    --blocking-only   Run only blocking integration checks after each fix (faster)
    --skip-chronic    Skip errors flagged as chronic (default: include them in attempt 4+)

Output:
    .tmp/logs/autonomous_fix_{timestamp}.log   — human-readable session report
    .tmp/logs/fix_history.json                  — cross-session metrics
    .tmp/errors_parsed.json                     — updated error list after session
"""

import argparse
import importlib.util as ilu
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR     = Path(__file__).parent.parent
TOOLS_DIR    = Path(__file__).parent
TMP_DIR      = BASE_DIR / ".tmp"
LOGS_DIR     = TMP_DIR / "logs"
PIPELINE_LOCK = TMP_DIR / "pipeline.lock"
HISTORY_FILE  = LOGS_DIR / "fix_history.json"
OUTPUT_FILE   = TMP_DIR / "errors_parsed.json"

MAX_ATTEMPTS = 5  # Escalating attempts before human queue


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_module(module_path: Path, *args) -> int:
    """Run a Python module as a subprocess. Returns exit code."""
    result = subprocess.run(
        [sys.executable, str(module_path)] + list(args),
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _load_module_direct(path: Path):
    """Load a Python module directly (in-process) for calling its functions."""
    spec = ilu.spec_from_file_location(path.stem, path)
    mod  = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cmd: list[str], capture: bool = True) -> tuple[int, str]:
    """Run a git command. Returns (returncode, stdout+stderr)."""
    result = subprocess.run(
        ["git"] + cmd,
        cwd=str(BASE_DIR),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


def _find_claude_exe() -> str:
    """
    Locate the Claude Code CLI executable. Tries PATH first, then known install locations.
    Returns the path to use (string), or raises FileNotFoundError if not found.
    """
    import shutil as _shutil
    import glob as _glob

    # 1. Try PATH first (ideal)
    found = _shutil.which("claude") or _shutil.which("claude.exe")
    if found:
        return found

    # 2. VS Code extension (most common on Windows — pick the highest version)
    vscode_pattern = str(Path.home() / ".vscode" / "extensions" /
                         "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    matches = sorted(_glob.glob(vscode_pattern))
    if matches:
        return matches[-1]  # highest version (last alphabetically)

    # 3. Windows Store app
    store_pattern = str(Path.home() / "AppData" / "Local" / "Packages" /
                        "Claude_*" / "LocalCache" / "Roaming" / "Claude" /
                        "claude-code" / "*" / "claude.exe")
    matches = sorted(_glob.glob(store_pattern))
    if matches:
        return matches[-1]

    raise FileNotFoundError("Claude Code CLI not found. Install from: https://claude.ai/code")


def _claude_print(prompt: str) -> tuple[bool, str]:
    """
    Call Claude Code CLI in non-interactive --print mode with a prompt.
    Returns (success, output_text).
    """
    try:
        claude_exe = _find_claude_exe()
    except FileNotFoundError as e:
        return False, str(e)

    try:
        result = subprocess.run(
            [claude_exe, "--print", prompt],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,  # 5 minute timeout per fix generation
        )
        output = result.stdout or ""
        if result.returncode != 0:
            stderr = result.stderr or ""
            return False, f"Claude CLI error (exit {result.returncode}): {stderr[:200]}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Claude CLI timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


def _apply_patch(tool_file: str, patch_text: str) -> bool:
    """
    Extract and apply a code block from Claude's response to the target file.
    Looks for ```python ... ``` blocks and writes the first one found.
    Returns True if a patch was applied, False if no code block found.

    Safety checks:
    - Rejects diff-format responses (# BEFORE: / # AFTER: patterns)
    - Rejects blocks shorter than the original file
    - Validates Python syntax before writing
    """
    import re
    # Match ```python ... ``` or ``` ... ```
    code_blocks = re.findall(r'```(?:python)?\n(.*?)```', patch_text, re.DOTALL)
    if not code_blocks:
        return False

    # Find the longest code block (most likely the full patched file)
    best_block = max(code_blocks, key=len)
    if len(best_block.strip()) < 50:
        return False  # Too short to be a real file

    # Reject diff/comment-format responses (Claude sometimes returns BEFORE/AFTER snippets)
    diff_markers = ["# BEFORE:", "# AFTER:", "--- a/", "+++ b/", "@@"]
    if any(marker in best_block for marker in diff_markers):
        print(f"  [!] _apply_patch rejected diff-format response for {tool_file} — "
              f"Claude returned a patch snippet, not a full file. Skipping.")
        return False

    target = BASE_DIR / tool_file
    if not target.exists():
        return False

    # Reject blocks significantly shorter than the original (likely a partial snippet)
    original_lines = target.read_text(encoding="utf-8", errors="replace").count("\n")
    new_lines = best_block.count("\n")
    if original_lines > 30 and new_lines < original_lines * 0.5:
        print(f"  [!] _apply_patch rejected: response is {new_lines} lines but original "
              f"is {original_lines} lines — likely a partial snippet, not a full file.")
        return False

    # Validate Python syntax before writing
    if tool_file.endswith(".py"):
        try:
            import ast
            ast.parse(best_block)
        except SyntaxError as e:
            print(f"  [!] _apply_patch rejected: syntax error in Claude's response: {e}")
            return False

    # Write the patched content
    target.write_text(best_block, encoding="utf-8")
    return True


def _run_integration_test(blocking_only: bool = True) -> dict:
    """
    Run integration_test.py and return the summary dict.
    Uses blocking_only=True for speed during fix attempts, full run at session end.
    """
    try:
        test_module = _load_module_direct(TOOLS_DIR / "integration_test.py")
        # Reset module-level results list between runs
        test_module._RESULTS.clear()
        test_module._START_TIME = time.time()
        return test_module.run_all(blocking_only=blocking_only)
    except Exception as e:
        return {
            "overall": "FAIL",
            "blocking_failures": 1,
            "soft_failures": 0,
            "blocking_fail_list": [{"name": "integration_test crashed", "detail": str(e)}],
            "soft_fail_list": [],
        }


# ── Fix prompt construction ────────────────────────────────────────────────────

def _read_file_safe(tool_file: str) -> str:
    path = BASE_DIR / tool_file
    if not path.exists():
        return f"[File not found: {tool_file}]"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Could not read {tool_file}: {e}]"


def _build_fix_prompt(err: dict, attempt: int, prior_failures: list[dict]) -> str:
    """
    Build a targeted fix prompt for Claude Code CLI.
    Escalates from surgical patch → caller fix → multi-file → architectural → rewrite.
    """
    tool_file = err["tool_file"]
    file_contents = _read_file_safe(tool_file)
    context_lines = "\n".join(err.get("context_lines", []))
    callers       = err.get("callers", [])
    caller_summary = "\n".join(
        f"  - {c['file']}:{c['line']} in {c['function']}()"
        for c in callers
    ) or "  (none found)"

    # Base error context — same for all attempts
    base_context = f"""You are fixing a bug in the LastMinuteDeals booking system (WAT framework).

CRITICAL OUTPUT RULE: You MUST output the COMPLETE corrected file as a single ```python code block.
Do NOT output a diff, patch, BEFORE/AFTER snippet, or partial code. The entire file content must
be inside the code block — not just the changed lines. Outputting a diff instead of the full file
will cause the fix to be REJECTED.

ERROR TO FIX:
  File:      {tool_file}
  Line:      {err['line']}
  Function:  {err['function_name']}
  Type:      {err['error_type']}
  Message:   {err['message']}

STACK TRACE:
{err.get('raw_trace', '[no trace]')}

CONTEXT (±20 lines around error):
{context_lines or '[no context available]'}

OTHER FILES THAT CALL THIS FUNCTION (preserve their API contracts):
{caller_summary}

FULL FILE CONTENTS ({tool_file}):
```python
{file_contents}
```
"""

    if prior_failures:
        failures_summary = "\n".join(
            f"  Attempt {f['attempt']}: {f['strategy']} → FAILED because: {f['regression']}"
            for f in prior_failures
        )
        base_context += f"""
PRIOR ATTEMPTS THAT CAUSED REGRESSIONS (DO NOT REPEAT THESE APPROACHES):
{failures_summary}
"""

    if attempt == 1:
        instruction = """TASK (Attempt 1 — Surgical patch):
Fix ONLY the exact lines causing this specific error. Make the smallest possible change.
Do NOT refactor, rename, or restructure anything else.
Do NOT modify other files.
Do NOT change the function signature or return type.
Output the complete corrected file as a single ```python code block."""

    elif attempt == 2:
        # Load caller files for context
        caller_contents = ""
        for c in callers[:2]:
            caller_contents += f"\nCALLER FILE: {c['file']}\n```python\n{_read_file_safe(c['file'])}\n```\n"

        instruction = f"""TASK (Attempt 2 — Caller-level fix):
The error-site patch keeps causing regressions. Instead, fix this at the CALLING level —
change how the broken function is called, not how it's implemented internally.
Look at the callers listed above and fix the invocation pattern there.
Do NOT change the broken function's implementation.
Do NOT change function signatures.
Output ONLY the corrected version of {tool_file} (fix the function to handle the calling issue).

{caller_contents}"""

    elif attempt == 3:
        # Include all caller file contents for multi-file fix
        caller_contents = ""
        for c in callers[:5]:
            caller_contents += f"\nCALLER FILE: {c['file']}\n```python\n{_read_file_safe(c['file'])}\n```\n"

        instruction = f"""TASK (Attempt 3 — Multi-file atomic fix):
Previous single-file patches caused regressions in callers. Fix BOTH the error site AND
all calling files simultaneously so the whole set is consistent.
Output each file as a separate ```python code block with a comment header:
  # FILE: tools/filename.py
  ```python
  [full file contents]
  ```

Files to fix: {tool_file} and any callers that need updating.
{caller_contents}

Rules:
- All code blocks must be complete files, not snippets
- Function signatures between files must be consistent
- Do NOT add new dependencies"""

    elif attempt == 4:
        instruction = f"""TASK (Attempt 4 — Architectural patch):
Simple patching has failed. The root cause is a design flaw.
Redesign the specific function/class that contains this bug to eliminate the root cause entirely.

Guidelines based on common patterns:
- Race conditions → use atomic operations (Supabase RPC, file locks, or compare-and-set)
- Initialization order bugs → use lazy initialization (compute on first access, not at import/module load)
- Tight coupling → introduce a thin adapter or wrapper function to decouple the dependency
- Fragile parsing → use a robust library (dateutil, json, etc.) instead of string manipulation
- Missing error handling → add explicit handling for each failure mode

CRITICAL CONSTRAINTS:
- Keep the EXACT SAME function signature and return type (callers must not need changes)
- Only redesign the specific function(s) involved in this error
- Do NOT refactor the whole file
- Do NOT add new top-level imports unless absolutely necessary

Output the complete corrected {tool_file} as a single ```python code block."""

    elif attempt == 5:
        instruction = f"""TASK (Attempt 5 — Targeted rewrite):
All partial fixes have failed. Rewrite the specific broken function from scratch.

The function to rewrite: `{err['function_name']}` in {tool_file}

Requirements:
1. IDENTICAL function signature (name, parameters, return type) — callers must NOT need any changes
2. Implement the correct behavior based on the docstring/context above
3. Handle all edge cases explicitly (don't assume inputs are well-formed)
4. Use only libraries already imported in the file — do NOT add new top-level imports
5. The rest of the file must remain UNCHANGED — only replace the function body

Output the complete corrected {tool_file} as a single ```python code block."""

    return base_context + "\n" + instruction


# ── Git operations ─────────────────────────────────────────────────────────────

def _make_backup_branch(error_id: str, attempt: int) -> str:
    """Create a git backup branch before applying a fix. Returns branch name."""
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name  = f"auto-fix/{ts}/{error_id[:8]}-attempt{attempt}"
    code, out = _git(["checkout", "-b", name])
    if code != 0:
        # Branch may already exist or we're in a detached state — just continue
        print(f"    [git] Warning creating branch: {out[:80]}")
    return name


def _revert_to_main() -> None:
    """Revert to main branch, discarding any uncommitted changes."""
    _git(["checkout", "main"])
    _git(["checkout", "--", "."])  # discard unstaged changes in working tree


def _commit_fix(error: dict, attempt: int, tool_file: str) -> bool:
    """Stage and commit the fix. Returns True on success."""
    code, _ = _git(["add", tool_file])
    if code != 0:
        return False
    msg = (f"auto-fix: {error['error_type']} in {Path(tool_file).name}:{error['line']} "
           f"(attempt {attempt})")
    code, out = _git(["commit", "-m", msg])
    return code == 0


# ── Session report ─────────────────────────────────────────────────────────────

class SessionReport:
    def __init__(self, dry_run: bool = False):
        self.dry_run     = dry_run
        self.start_time  = datetime.now(timezone.utc)
        self.fixed:    list[dict] = []
        self.reverted: list[dict] = []
        self.human_q:  list[dict] = []
        self.skipped_chronic: list[str] = []

    def add_fixed(self, error: dict, attempt: int, elapsed: float) -> None:
        self.fixed.append({
            "file":    error["tool_file"],
            "line":    error["line"],
            "type":    error["error_type"],
            "attempt": attempt,
            "elapsed": round(elapsed, 1),
        })

    def add_reverted(self, error: dict, attempt: int, regression: str) -> None:
        self.reverted.append({
            "file":      error["tool_file"],
            "line":      error["line"],
            "type":      error["error_type"],
            "attempt":   attempt,
            "regression": regression,
        })

    def add_human_queue(self, error: dict, all_attempts: list[dict]) -> None:
        self.human_q.append({
            "file":     error["tool_file"],
            "line":     error["line"],
            "type":     error["error_type"],
            "message":  error["message"],
            "attempts": all_attempts,
        })

    def render(self, final_test: dict | None = None) -> str:
        elapsed_total = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        lines = []
        ts    = self.start_time.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"=== Autonomous Fix Session: {ts} {'[DRY RUN]' if self.dry_run else ''} ===")
        lines.append(f"Duration: {elapsed_total:.0f}s")
        lines.append("")

        total_errors = len(self.fixed) + len(self.reverted) + len(self.human_q)
        lines.append(f"Errors addressed:  {total_errors}")
        lines.append(f"Fixed & verified:  {len(self.fixed)}  "
                     f"({'%.0f' % (len(self.fixed)/total_errors*100) if total_errors else 0}%)")
        lines.append(f"Reverted:          {len(self.reverted)}")
        lines.append(f"Human queue:       {len(self.human_q)}")

        if self.fixed:
            lines.append("\nFixed:")
            for f in self.fixed:
                lines.append(f"  ✓ {f['file']}:{f['line']} — {f['type']} (attempt {f['attempt']}, {f['elapsed']}s)")

        if self.reverted:
            lines.append("\nReverted (fix caused regression — left unchanged):")
            for r in self.reverted:
                lines.append(f"  ✗ {r['file']}:{r['line']} — {r['type']}")
                lines.append(f"       Regression: {r['regression'][:120]}")

        if self.human_q:
            lines.append("\nNeeds Human Review:")
            for h in self.human_q:
                lines.append(f"  ✗ {h['file']}:{h['line']} — {h['type']}: {h['message'][:80]}")
                for a in h["attempts"]:
                    lines.append(f"       Attempt {a['attempt']} ({a['strategy']}): {a['regression'][:100]}")
                # Generate a crisp human decision prompt
                lines.append(f"       → What human needs to decide: review the 5 failed approaches above")
                lines.append(f"         and determine if this requires a product/design decision vs a code fix.")

        if final_test:
            lines.append(f"\nFinal full-surface test:")
            lines.append(f"  Overall:          {final_test['overall']}")
            lines.append(f"  Blocking failures: {final_test['blocking_failures']}")
            lines.append(f"  Soft failures:     {final_test['soft_failures']}")
            if final_test.get("blocking_fail_list"):
                for bf in final_test["blocking_fail_list"]:
                    lines.append(f"    ✗ {bf['name']}: {bf['detail']}")

        if self.reverted and total_errors > 0:
            revert_rate = len(self.reverted) / total_errors * 100
            if revert_rate > 50:
                lines.append(f"\n⚠ STRUGGLING: {revert_rate:.0f}% revert rate. "
                              f"System may have architectural issues requiring supervised session.")

        lines.append("")
        return "\n".join(lines)

    def save(self) -> Path:
        ts_str = self.start_time.strftime("%Y%m%d-%H%M%S")
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"autonomous_fix_{ts_str}.log"
        path.write_text(self.render(), encoding="utf-8")
        # Also write as "latest" for easy reference
        latest = LOGS_DIR / "autonomous_fix_latest.log"
        latest.write_text(self.render(), encoding="utf-8")
        return path


# ── Fix history persistence ────────────────────────────────────────────────────

def _load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record_fix_in_history(history: dict, error: dict, success: bool,
                            attempt: int, regression: str = "") -> None:
    eid = error["error_id"]
    if eid not in history:
        history[eid] = {"sessions_seen": 0, "sessions": [], "prior_fix_attempts": []}
    if success:
        history[eid]["fixed_at"] = datetime.now(timezone.utc).isoformat()
        history[eid]["fixed_attempt"] = attempt
        history[eid]["prior_fix_attempts"] = []
    else:
        history[eid]["prior_fix_attempts"].append({
            "attempt":    attempt,
            "strategy":   _attempt_strategy_name(attempt),
            "regression": regression,
            "ts":         datetime.now(timezone.utc).isoformat(),
        })
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def _attempt_strategy_name(attempt: int) -> str:
    return {
        1: "Surgical patch at error site",
        2: "Caller-level fix",
        3: "Multi-file atomic fix",
        4: "Architectural patch / redesign",
        5: "Targeted rewrite of broken function",
    }.get(attempt, f"Attempt {attempt}")


# ── Wait for pipeline lock ────────────────────────────────────────────────────

def _wait_for_pipeline(max_wait: int = 600) -> bool:
    """Wait up to max_wait seconds for pipeline.lock to clear. Returns True if safe to proceed."""
    if not PIPELINE_LOCK.exists():
        return True
    print(f"  Pipeline lock detected ({PIPELINE_LOCK}). Waiting up to {max_wait}s...")
    waited = 0
    while PIPELINE_LOCK.exists() and waited < max_wait:
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            print(f"  Still waiting ({waited}s / {max_wait}s)...")
    if PIPELINE_LOCK.exists():
        print(f"  Pipeline still locked after {max_wait}s — aborting session.")
        return False
    print(f"  Pipeline lock cleared after {waited}s.")
    return True


# ── Core fix loop ──────────────────────────────────────────────────────────────

def fix_one_error(error: dict, report: SessionReport, dry_run: bool,
                  blocking_only: bool, history: dict) -> bool:
    """
    Attempt to fix a single error using up to MAX_ATTEMPTS strategies.
    Returns True if successfully fixed and committed, False otherwise.
    """
    tool_file    = error["tool_file"]
    error_id     = error["error_id"]
    all_attempts: list[dict] = []

    # Determine starting attempt (resume from where prior session failed)
    prior_attempts = error.get("prior_fix_attempts", [])
    start_attempt  = len(prior_attempts) + 1
    if start_attempt > MAX_ATTEMPTS:
        print(f"  [SKIP] Already exhausted {MAX_ATTEMPTS} attempts in prior sessions.")
        report.add_human_queue(error, prior_attempts)
        return False

    for attempt in range(start_attempt, MAX_ATTEMPTS + 1):
        strategy = _attempt_strategy_name(attempt)
        t_start  = time.time()

        print(f"\n  Attempt {attempt}/{MAX_ATTEMPTS}: {strategy}")

        # Build the fix prompt
        prompt = _build_fix_prompt(error, attempt, all_attempts + prior_attempts)

        if dry_run:
            print(f"    [DRY RUN] Would send {len(prompt)} char prompt to Claude Code CLI")
            print(f"    [DRY RUN] Prompt preview: {prompt[:200]}...")
            continue

        # Create git backup branch
        branch = _make_backup_branch(error_id, attempt)
        print(f"    Created backup branch: {branch}")

        # Call Claude Code CLI
        print(f"    Calling Claude Code CLI ({len(prompt)} char prompt)...")
        success, claude_output = _claude_print(prompt)
        if not success:
            print(f"    Claude CLI failed: {claude_output[:120]}")
            _revert_to_main()
            all_attempts.append({
                "attempt": attempt, "strategy": strategy,
                "regression": f"Claude CLI failed: {claude_output[:120]}"
            })
            _record_fix_in_history(history, error, False, attempt,
                                   f"Claude CLI failed: {claude_output[:80]}")
            continue

        # Apply the patch to the file
        patched = _apply_patch(tool_file, claude_output)
        if not patched:
            print(f"    No code block found in Claude's response — skipping")
            _revert_to_main()
            all_attempts.append({
                "attempt": attempt, "strategy": strategy,
                "regression": "Claude returned no code block — could not apply patch"
            })
            _record_fix_in_history(history, error, False, attempt,
                                   "No code block in Claude response")
            continue

        print(f"    Patch applied to {tool_file}. Running integration test...")

        # Run integration test
        test_result = _run_integration_test(blocking_only=True)

        if test_result["overall"] == "PASS":
            elapsed = time.time() - t_start
            # Commit the fix
            committed = _commit_fix(error, attempt, tool_file)
            # Switch back to main and merge
            _git(["checkout", "main"])
            merge_code, merge_out = _git(["merge", "--no-ff", branch,
                                          "-m", f"Merge auto-fix/{error_id[:8]}-attempt{attempt}"])
            if merge_code != 0:
                print(f"    Merge failed: {merge_out[:80]} — cherry-picking instead")
                _git(["cherry-pick", branch])

            print(f"    ✓ Fix verified and merged in {elapsed:.0f}s")
            report.add_fixed(error, attempt, elapsed)
            _record_fix_in_history(history, error, True, attempt)
            return True
        else:
            # Fix caused a regression — revert
            fail_list = test_result.get("blocking_fail_list", [])
            regression_detail = "; ".join(
                f"{f['name']}: {f['detail']}" for f in fail_list
            )[:200]
            print(f"    ✗ Integration test FAILED. Reverting.")
            print(f"      Regressions: {regression_detail[:120]}")

            _git(["checkout", "main"])
            _git(["branch", "-D", branch])  # delete the fix branch

            all_attempts.append({
                "attempt": attempt, "strategy": strategy,
                "regression": regression_detail
            })
            _record_fix_in_history(history, error, False, attempt, regression_detail)
            report.add_reverted(error, attempt, regression_detail)

    # All attempts exhausted
    print(f"\n  All {MAX_ATTEMPTS} attempts exhausted — adding to human queue")
    report.add_human_queue(error, all_attempts + prior_attempts)
    return False


# ── Post-fix documentation ────────────────────────────────────────────────────

def _document_session_fixes(report: SessionReport) -> None:
    """
    After each fix session, update SYSTEM_MAP.md, docs/bug_audit_log.md, and
    the project_system_state.md memory file so the next Claude session has full context.

    Protocol matches the existing documentation pattern:
    - SYSTEM_MAP.md: mark fixed bugs in the gap table
    - docs/bug_audit_log.md: append session block (same format as existing sessions)
    - memory/project_system_state.md: one-line summary for fast orientation
    """
    if not report.fixed:
        return  # Nothing fixed — no documentation needed

    # Build the documentation prompt
    fixed_list = "\n".join(
        f"  - {f['file']}:{f['line']} — {f['type']} (attempt {f['attempt']})"
        for f in report.fixed
    )
    human_q_list = "\n".join(
        f"  - {h['file']}:{h['line']} — {h['type']}: {h['message'][:60]}"
        for h in report.human_q
    ) or "  (none)"

    session_date = report.start_time.strftime("%Y-%m-%d")
    session_ts   = report.start_time.strftime("%Y-%m-%d %H:%M UTC")

    # Get git commit hashes for fixed files (to reference in docs)
    _, recent_commits = _git(["log", "--oneline", "-10"])

    doc_prompt = f"""You are updating documentation after an autonomous bug-fix session in the
LastMinuteDeals WAT framework codebase.

SESSION SUMMARY ({session_ts}):
  Fixed & verified ({len(report.fixed)} bugs):
{fixed_list}

  Still needs human review ({len(report.human_q)} bugs):
{human_q_list}

Recent git commits (for reference):
{recent_commits}

TASK: Make the following 3 documentation updates. Output each as a clearly labeled section.

1. SYSTEM_MAP.md UPDATE:
   Read the current SYSTEM_MAP.md, find the known gaps/bugs table, and mark each fixed bug
   with: FIXED (autonomous agent, {session_date}, commit: <hash from git log>)
   Only modify the rows for bugs that were actually fixed in this session.
   Output the updated SYSTEM_MAP.md as a complete file.

2. docs/bug_audit_log.md APPEND:
   Append a new session block to the existing bug_audit_log.md using the same format as
   existing session blocks (check the file for the format). Include:
   - Session date: {session_date}
   - Bugs fixed (with file, line, type, fix description)
   - Bugs escalated to human queue (with reason)
   Output ONLY the new block to append (not the full file).

3. memory/project_system_state.md UPDATE:
   The memory file is at: C:\\Users\\janaa\\.claude\\projects\\c--Users-janaa-Agentic-Workflows\\memory\\project_system_state.md
   Add a single line to the "What's working" or update the relevant section to reflect
   the newly fixed bugs. Keep it concise — one line per fixed bug maximum.
   Output the updated memory file content.

Read the actual file contents before making changes so your updates are accurate.
"""

    print("\nUpdating documentation (SYSTEM_MAP.md, bug_audit_log.md, memory)...")
    success, claude_output = _claude_print(doc_prompt)
    if not success:
        print(f"  Documentation update failed: {claude_output[:80]}")
        return

    # Apply SYSTEM_MAP.md update
    if "SYSTEM_MAP.md" in claude_output:
        _apply_patch("SYSTEM_MAP.md", claude_output)
        _git(["add", "SYSTEM_MAP.md"])
        print("  Updated SYSTEM_MAP.md")

    # Apply bug_audit_log.md append
    audit_path = BASE_DIR / "docs" / "bug_audit_log.md"
    if audit_path.exists() and "bug_audit_log" in claude_output.lower():
        # Extract the append block — look for it after the SYSTEM_MAP code block
        import re
        blocks = re.findall(r'```(?:markdown|md)?\n(.*?)```', claude_output, re.DOTALL)
        # The audit log block is typically shorter than SYSTEM_MAP (which is the whole file)
        append_blocks = [b for b in blocks if len(b) < 3000 and "##" in b]
        if append_blocks:
            block = append_blocks[0]
            existing = audit_path.read_text(encoding="utf-8", errors="replace")
            audit_path.write_text(existing + "\n" + block, encoding="utf-8")
            _git(["add", "docs/bug_audit_log.md"])
            print("  Appended to docs/bug_audit_log.md")

    # Apply memory file update
    memory_path = Path(r"C:\Users\janaa\.claude\projects\c--Users-janaa-Agentic-Workflows\memory\project_system_state.md")
    if memory_path.exists() and "project_system_state" in claude_output.lower():
        blocks = re.findall(r'```(?:markdown|md)?\n(.*?)```', claude_output, re.DOTALL)
        state_blocks = [b for b in blocks if "working" in b.lower() or "pending" in b.lower()]
        if state_blocks:
            memory_path.write_text(state_blocks[0], encoding="utf-8")
            print("  Updated memory/project_system_state.md")

    # Commit documentation changes
    code, _ = _git(["diff", "--cached", "--quiet"])
    if code != 0:  # there are staged changes
        _git(["commit", "-m", f"docs: autonomous fix session {session_date} — {len(report.fixed)} bugs fixed"])
        print("  Documentation committed.")


# ── Session entry point ────────────────────────────────────────────────────────

def run_session(args) -> SessionReport:
    report   = SessionReport(dry_run=args.dry_run)
    history  = _load_history()

    print(f"\n{'='*60}")
    print(f"  Autonomous Fix Session — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if args.dry_run:
        print("  Mode: DRY RUN (no changes applied)")
    print(f"{'='*60}")

    # Step 1: Wait for pipeline lock
    if not args.dry_run and not _wait_for_pipeline():
        print("Aborting — pipeline is locked.")
        return report

    # Step 2: Baseline integration test
    print("\nRunning baseline integration test...")
    baseline = _run_integration_test(blocking_only=True)
    print(f"  Baseline: {baseline['overall']} "
          f"({baseline['blocking_failures']} blocking failures)")

    # Step 3: Parse errors
    print("\nParsing error logs...")
    parse_module = _load_module_direct(TOOLS_DIR / "parse_errors.py")
    kwargs = {}
    if hasattr(args, "since_hours"):
        kwargs["since_hours"] = args.since_hours
    errors = parse_module.parse_logs(**kwargs)

    # Filter to specific file if requested
    if args.file:
        target = args.file.replace("\\", "/").lstrip("./")
        errors = [e for e in errors if target in e["tool_file"]]
        print(f"  Filtered to {target}: {len(errors)} errors")

    if not errors:
        print("  No errors found — system appears healthy.")
        return report

    # Separate chronic from actionable (skip_chronic option)
    actionable = [e for e in errors if not e.get("chronic")]
    chronic    = [e for e in errors if e.get("chronic")]

    print(f"  Actionable: {len(actionable)}, Chronic (structural): {len(chronic)}")

    if not args.skip_chronic and chronic:
        print(f"  Including {len(chronic)} chronic errors starting at attempt 4 (architectural patch)")
        # For chronic errors, force start at attempt 4
        for e in chronic:
            e["prior_fix_attempts"] = e.get("prior_fix_attempts", []) + [
                {"attempt": 1, "strategy": "Surgical patch",    "regression": "chronic — skipped"},
                {"attempt": 2, "strategy": "Caller-level fix",  "regression": "chronic — skipped"},
                {"attempt": 3, "strategy": "Multi-file atomic", "regression": "chronic — skipped"},
            ]
        actionable = actionable + chronic

    # Limit errors per session
    to_fix = actionable[:args.max_errors]
    print(f"  Addressing {len(to_fix)} error(s) this session (max={args.max_errors})")

    # Step 4: Fix each error
    for i, error in enumerate(to_fix, 1):
        print(f"\n[{i}/{len(to_fix)}] {error['tool_file']}:{error['line']} "
              f"— {error['error_type']}: {error['message'][:60]} "
              f"[{error['frequency']}x{'  CHRONIC' if error.get('chronic') else ''}]")
        fix_one_error(error, report, dry_run=args.dry_run,
                      blocking_only=args.blocking_only, history=history)

    # Step 5: Final full-surface test
    print("\nRunning final full-surface integration test...")
    final_test = _run_integration_test(blocking_only=False)

    # Step 6: Save session report
    report_text = report.render(final_test=final_test)
    print(report_text)

    if not args.dry_run:
        saved = report.save()
        print(f"Session report saved to: {saved}")

        # Step 7: Update SYSTEM_MAP.md, bug_audit_log.md, and memory
        _document_session_fixes(report)

        # Re-run parse_errors to refresh the output file with updated history
        parse_module.parse_logs(since_hours=args.since_hours)

    return report


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous bug-fix loop for LastMinuteDeals"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate fix prompts but do not apply or commit anything")
    parser.add_argument("--max-errors", type=int, default=5,
                        help="Max errors to fix per session (default: 5)")
    parser.add_argument("--file", type=str, default=None,
                        help="Only fix errors in this file (e.g. tools/fetch_octo_slots.py)")
    parser.add_argument("--since-hours", type=float, default=8.0,
                        help="Parse errors from the last N hours (default: 8)")
    parser.add_argument("--blocking-only", action="store_true",
                        help="Use blocking-only mode for integration tests (faster)")
    parser.add_argument("--skip-chronic", action="store_true",
                        help="Skip errors flagged as chronic (3+ sessions)")
    args = parser.parse_args()

    run_session(args)


if __name__ == "__main__":
    main()
