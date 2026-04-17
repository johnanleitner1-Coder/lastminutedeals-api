# Autonomous Bug Fix Workflow

## Objective
Automatically detect, fix, and verify bugs in the LastMinuteDeals codebase without human supervision. Every fix is tested against the full system before being committed, and any fix that causes a regression is automatically reverted.

## Inputs Required
- None (runs automatically post-pipeline) or:
- Optionally: `--file tools/specific_file.py` to target a specific file
- Optionally: `--since-hours N` to narrow the error time window

## Tools Used
- `tools/parse_errors.py` — extracts structured errors from pipeline.log / http_server.log
- `tools/integration_test.py` — full-surface smoke test (28+ checks, all read-only)
- `tools/run_autonomous_fix.py` — orchestrates the fix loop with 5-attempt escalation + auto-revert

---

## When to Run

### Automatic (Recommended)
Add as a post-step to the existing 4-hour pipeline scheduler:

**Windows Task Scheduler** (after the current `run_pipeline.bat` task):
```
python tools/run_autonomous_fix.py --max-errors 3 --blocking-only
```

**Railway APScheduler** (add to `run_api_server.py`'s scheduler):
```python
scheduler.add_job(
    func=lambda: subprocess.run(["python", "tools/run_autonomous_fix.py",
                                  "--max-errors", "3", "--blocking-only"]),
    trigger="interval",
    hours=4,
    id="autonomous_bug_fix",
    misfire_grace_time=900,
)
```

### Manual (On-Demand)
```bash
# Full run — all detected errors, all surfaces
python tools/run_autonomous_fix.py

# Target a specific file only
python tools/run_autonomous_fix.py --file tools/fetch_octo_slots.py

# Dry run — see what would happen, no changes applied
python tools/run_autonomous_fix.py --dry-run

# Fast mode — blocking checks only (skip social/cloud/external API checks)
python tools/run_autonomous_fix.py --blocking-only --max-errors 5

# Just run the integration test (no fixing)
python tools/integration_test.py

# Just parse errors (no fixing)
python tools/parse_errors.py --since-hours 24
```

---

## Fix Strategy (5 Escalating Attempts)

For each detected error, the loop tries progressively broader approaches:

| Attempt | Strategy | When Used |
|---|---|---|
| 1 | **Surgical patch** — fix only the exact error lines | First try on every error |
| 2 | **Caller-level fix** — change how the function is invoked | Attempt 1 caused regression |
| 3 | **Multi-file atomic fix** — fix error site AND callers simultaneously | Attempts 1-2 caused regressions |
| 4 | **Architectural patch** — redesign the specific function/class | Attempts 1-3 all caused regressions |
| 5 | **Targeted rewrite** — rewrite the broken function from scratch, same API | All prior attempts failed |
| — | **Human queue** — escalate with precise brief | All 5 attempts caused regressions |

Each attempt:
1. Creates a git backup branch before touching any file
2. Calls `claude --print` with a carefully crafted prompt
3. Applies the generated patch
4. Runs `integration_test.py --blocking-only`
5. If PASS → commits and merges; if FAIL → reverts branch, adds context to next attempt

---

## Integration Test Coverage

The integration test (`tools/integration_test.py`) checks 6 surfaces after every fix:

**BLOCKING (failure reverts the fix):**
- Core Python pipeline: aggregated slots schema, normalize_slot, circuit breaker, wallet store
- Supabase: slots table, bookings table, wallets storage, circuit breaker storage, no test slots in prod

**NON-BLOCKING (logged but don't revert a good fix — pre-existing issues):**
- MCP server: search_slots, get_slot, get_booking_status, book_slot registration
- External APIs: Stripe, SendGrid, Twilio, Google Sheets OAuth, Bokun, Rezdy, Eventbrite, Meetup
- Cloud infrastructure: Railway /health, GitHub Pages, Cloudflare DNS
- Social: Telegram, Twitter, Reddit

---

## Reading the Session Report

Reports are written to `.tmp/logs/autonomous_fix_{timestamp}.log` and `.tmp/logs/autonomous_fix_latest.log`.

**Example report:**
```
=== Autonomous Fix Session: 2026-04-17 14:00 UTC ===
Duration: 22s

Errors addressed:  8
Fixed & verified:  7  (87%)
Reverted:          0
Human queue:       1

Fixed:
  ✓ fetch_octo_slots.py:200 — HTTPError (attempt 1, 38s)
  ✓ fetch_eventbrite_slots.py:250 — ValueError (attempt 1, 41s)
  ✓ execution_engine.py:641 — AttributeError (attempt 4, 84s)

Needs Human Review:
  ✗ run_api_server.py:111 — LMD_SIGNING_SECRET persistence
       Attempt 1: broke Supabase wallets check
       Attempt 2: broke circuit breaker read
       Attempt 3: Flask startup sequence failure
       Attempt 4: Race condition between workers
       Attempt 5: Storage read timing issue
       → Human needs to decide: product/design decision required
```

**What each section means:**

| Field | Meaning |
|---|---|
| Fixed & verified | Committed to main. Integration test passed after fix. |
| Reverted | Fix caused a regression. File unchanged. Next attempt tries a different strategy. |
| Human queue | 5 approaches exhausted. Requires a human decision or supervised session. |
| Revert rate >50% | Warning: system may have deeper architectural issues. Consider a supervised session. |

---

## Handling the Human Queue

When an error appears in the human queue, the report includes:
- All 5 approaches tried and why each failed
- A statement of what specifically the human needs to decide

**How to resolve:**
1. Read the error brief in the session report
2. Make the design decision (e.g., "LMD_SIGNING_SECRET → add to Railway env vars permanently")
3. Implement the fix manually or in a supervised Claude session scoped to only that file
4. Run `python tools/run_autonomous_fix.py --file tools/affected_file.py` to verify

---

## Chronic Errors

An error becomes **chronic** when it appears in 3+ consecutive sessions without being fixed. Chronic errors are automatically skipped to attempts 4-5 (architectural + rewrite) since surgical patches clearly haven't worked.

If a chronic error persists through attempts 4-5, it's a signal that the system has a structural issue that may require a supervised architectural session rather than automated patching.

**Check chronic errors:**
```bash
python tools/parse_errors.py --since-hours 72
cat .tmp/errors_parsed.json | python -c "
import json,sys
errors = json.load(sys.stdin)
chronic = [e for e in errors if e.get('chronic')]
print(f'{len(chronic)} chronic errors:')
for e in chronic: print(f'  {e[\"tool_file\"]}:{e[\"line\"]} — {e[\"message\"][:60]}')
"
```

---

## Cross-Session Quality Metrics

The fix history is stored in `.tmp/logs/fix_history.json`. It tracks:

| Metric | Description |
|---|---|
| Fix durability | Do fixes survive the next pipeline run or do the same errors come back? |
| Error trend | Total errors per session over time (should decrease) |
| Revert rate | Percentage of fix attempts that caused regressions (healthy: <30%) |
| Chronic count | Number of errors that have resisted 3+ sessions of fixes |

The loop flags `⚠ STRUGGLING` in the report if revert rate exceeds 50% in a session.

---

## Adjusting Behavior

**Tune the error window:**
```bash
# Only fix errors from the last 4 hours (reduces noise from old errors)
python tools/run_autonomous_fix.py --since-hours 4
```

**Reduce scope for stability:**
```bash
# Fix at most 2 errors per run (safer for volatile pipelines)
python tools/run_autonomous_fix.py --max-errors 2
```

**Exclude a file temporarily (e.g., while you're editing it):**
The loop will skip errors in files that have uncommitted changes — it detects this via `git status` before applying a patch.

**Reset a chronic error to retry from attempt 1:**
Edit `.tmp/logs/fix_history.json`, find the error_id entry, and set `"sessions_seen": 0` and `"prior_fix_attempts": []`.

---

## Error Handling

| Scenario | Loop Behavior |
|---|---|
| Pipeline lock detected | Wait up to 10 minutes, abort if still locked |
| Claude CLI not found | Error logged, session aborts — ensure Claude Code is installed |
| Claude CLI times out (>5 min) | Attempt logged as failed, move to next attempt |
| No code block in Claude's response | Attempt skipped, move to next strategy |
| Git operation fails | Attempt logged as failed, file left unchanged |
| Integration test crashes | Treated as FAIL, fix reverted |
| Supabase unreachable during test | Treated as blocking failure, fix reverted |

---

## Documentation After Each Session

After every fix session (when at least one bug was fixed), the loop automatically:

1. **Updates `SYSTEM_MAP.md`** — marks each fixed bug in the known gaps/bugs table with:
   `FIXED (autonomous agent, YYYY-MM-DD, commit: <hash>)`

2. **Appends to `docs/bug_audit_log.md`** — adds a session block in the same format as existing sessions (bug #, severity, file, description, fix)

3. **Updates `memory/project_system_state.md`** — adds a one-line summary so the next Claude session knows what changed without reading the full SYSTEM_MAP

4. **Commits documentation** — all doc changes in a single commit: `docs: autonomous fix session YYYY-MM-DD — N bugs fixed`

This means every session is fully self-documenting. When you bring Claude into a new conversation, reading `SYSTEM_MAP.md` (per protocol) will show exactly which bugs are closed, which are open, and what changed.

---

## Self-Improvement Loop

This workflow improves over time:
1. **Session 1**: Fixes easy bugs (wrong status codes, missing try/except, off-by-one)
2. **Session 2**: Fixes medium bugs that persisted (rate limiting, pagination gaps)
3. **Session 3+**: Chronic architectural issues surfaced for human review
4. **Human review**: Design decisions made, one supervised session, fixes committed
5. **Next sessions**: System is cleaner, fewer errors, lower revert rate

The loop doesn't replace understanding — it handles the mechanical work so supervised sessions can focus on the 1-2 genuinely hard problems that require judgment.
