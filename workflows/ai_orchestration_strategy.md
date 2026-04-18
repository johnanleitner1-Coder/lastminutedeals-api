# AI Orchestration Strategy — Running 24/7 Without Token Limits

## Core Principle

The pipeline is designed so that **zero LLM tokens are consumed per run**. All scheduled tasks are deterministic Python scripts. Claude (or any AI) is only used to build/fix/improve the system — not to operate it.

## Token Consumption Map

| Task | Who Does It | LLM Tokens Used |
|------|-------------|-----------------|
| Fetch slots (OCTO) | Python script | 0 |
| Aggregate + deduplicate | Python script | 0 |
| Compute pricing | Python script (rule-based) | 0 |
| Sync to Supabase | Python script | 0 |
| Serve API / MCP | Python server (Flask) | 0 |
| Scheduled pipeline trigger | Railway APScheduler | 0 |
| **Fix a broken tool** | Claude Code (you + Claude) | Some |
| **Add a new supplier** | Claude Code (you + Claude) | Some |
| **Update pricing coefficients** | Claude Code (occasional) | Some |

**Result: The system runs 24/7 forever with zero ongoing AI costs.**

---

## When to Involve Claude

Use Claude Code only for:
1. **Debugging sessions** — when a tool breaks and you need to diagnose + fix it
2. **Strategic decisions** — adding a new supplier, adjusting pricing strategy
3. **Weekly review** (optional) — review health endpoint, check Supabase slot counts
4. **New features** — building something new on top of the working pipeline

**Frequency: once a week or less once the system is running.**

---

## Pipeline Architecture

```
Every 4 hours (Railway APScheduler):
  _run_slot_discovery()
    ├── python tools/fetch_octo_slots.py --hours-ahead 168
    ├── python tools/aggregate_slots.py --hours-ahead 168
    ├── python tools/compute_pricing.py
    └── python tools/sync_to_supabase.py
```

No AI SDK calls in any of the above. 100% deterministic. Zero token cost.

---

## Summary

- **Pipeline operation**: 100% free, no AI tokens, runs forever via Railway scheduler
- **Tool building + debugging**: Use Claude Code in focused sessions (weekly or as needed)
- **API/MCP serving**: Deterministic Flask/FastMCP, zero AI cost per request
