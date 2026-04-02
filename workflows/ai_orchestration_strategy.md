# AI Orchestration Strategy — Running 24/7 Without Token Limits

## Core Principle

The pipeline is designed so that **zero LLM tokens are consumed per run**. All scheduled tasks are deterministic Python scripts. Claude (or any AI) is only used to build/fix/improve the system — not to operate it.

## Token Consumption Map

| Task | Who Does It | LLM Tokens Used |
|------|-------------|-----------------|
| Fetch slots (all platforms) | Python scripts | 0 |
| Aggregate + deduplicate | Python script | 0 |
| Compute pricing | Python script (rule-based) | 0 |
| Write to Google Sheets | Python script | 0 |
| Generate landing page HTML | Python script (template) | 0 |
| Post to Twitter/Reddit/Telegram | Python scripts | 0 |
| Scheduled cron trigger | Windows Task Scheduler | 0 |
| **Fix a broken tool** | Claude Code (you + Claude) | Some |
| **Write a new platform fetcher** | Claude Code (you + Claude) | Some |
| **Update pricing coefficients** | Claude Code (occasional) | Some |

**Result: The system runs 24/7 forever with zero ongoing AI costs.**

---

## Where Local AI Fits (Optional Enhancements)

For tasks that benefit from language generation but don't need Claude's quality level, use a local model (Ollama + Mistral/Llama). These run free on your machine:

### 1. Post Copy Generation (local AI)
Instead of hardcoded templates, a local model writes varied social posts:
```
Input:  slot JSON (service, city, time, price)
Output: tweet text, Reddit post title, Telegram message
Model:  Ollama mistral:7b or llama3:8b (runs locally, free)
```
Tool: `tools/generate_post_copy.py` — calls Ollama API at `http://localhost:11434`

### 2. Pricing Narrative (local AI, optional)
Generate a one-line deal description from slot data:
- "Rare last-minute yoga slot in Manhattan — usually books weeks out"
- Used on landing page cards for higher conversion

### 3. Platform Error Classification (local AI)
If a scraper returns unexpected HTML, a local model classifies whether it's:
- A Cloudflare block (retry later)
- A 404 (listing removed, delete from seed)
- A rate limit (back off)
- A schema change (alert Claude Code needed)

---

## Ollama Setup (one-time, free)

```bash
# Install Ollama (Windows): https://ollama.com/download
# Pull a small fast model:
ollama pull mistral:7b

# Test:
curl http://localhost:11434/api/generate -d '{
  "model": "mistral:7b",
  "prompt": "Write a 280-char tweet for: 60-min massage in NYC, $95, available in 4 hours",
  "stream": false
}'
```

Ollama runs as a background service — no GPU required (CPU works fine for 7B models on a modern laptop).

---

## When to Involve Claude

Use Claude Code only for:
1. **Initial build sessions** (like now) — building new tools
2. **Debugging sessions** — when a tool breaks and you need to diagnose + fix it
3. **Strategic decisions** — adding a new platform, redesigning a revenue stream
4. **Weekly review** (optional) — review run logs, adjust pricing coefficients, plan next feature

**Frequency: once a week or less once the system is running.**

---

## 24/7 Scheduling Architecture

```
Every 4 hours (Task Scheduler):
  run_pipeline.bat
    ├── python tools/fetch_mindbody_slots.py
    ├── python tools/fetch_airbnb_ical_slots.py --mode slots
    ├── python tools/fetch_eventbrite_slots.py
    ├── python tools/aggregate_slots.py
    ├── python tools/compute_pricing.py
    ├── python tools/generate_affiliate_links.py
    ├── python tools/write_to_sheets.py
    ├── python tools/update_landing_page.py
    └── python tools/post_to_telegram.py
        python tools/post_to_twitter.py
        python tools/post_to_reddit.py

Once weekly (Task Scheduler):
  refresh_seeds.bat
    ├── python tools/fetch_airbnb_ical_slots.py --mode seed --max-cities 30
    └── [future: refresh other platform seeds]
```

No Python AI SDK calls in any of the above. 100% deterministic. Zero token cost.

---

## Hybrid Agent Design (Advanced)

For tasks where you want AI assistance but don't want to hit Claude limits, use this pattern:

```python
# In any tool that needs AI assistance:
def get_ai_response(prompt: str, prefer_local: bool = True) -> str:
    if prefer_local:
        # Try local Ollama first (free, fast, private)
        try:
            resp = requests.post('http://localhost:11434/api/generate',
                json={'model': 'mistral:7b', 'prompt': prompt, 'stream': False},
                timeout=30)
            return resp.json()['response']
        except Exception:
            pass

    # Fall back to Claude API only if local fails or prefer_local=False
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    msg = client.messages.create(model='claude-haiku-4-5-20251001',  # cheapest
                                  max_tokens=500, messages=[{'role': 'user', 'content': prompt}])
    return msg.content[0].text
```

**Key**: Use `claude-haiku-4-5-20251001` (not Sonnet/Opus) for any automated AI calls — it's ~20x cheaper and perfectly capable for copy generation and classification tasks.

---

## Cost Estimate If You Do Use Claude API

If you add automated Claude calls for post copy (not required):
- 1 post generated = ~200 input + 100 output tokens
- Haiku pricing: ~$0.001 per 1,000 tokens
- 20 posts/day × 300 tokens = 6,000 tokens/day = **~$0.006/day = $0.18/month**

Effectively free. But local Ollama is literally $0.

---

## Summary

- **Pipeline operation**: 100% free, no AI tokens, runs forever via Task Scheduler
- **Post copy + minor AI tasks**: Use local Ollama (free, private, no API limits)
- **Tool building + debugging**: Use Claude Code in focused sessions (weekly or as needed)
- **Emergency smart decisions**: Claude API with Haiku (negligible cost)
