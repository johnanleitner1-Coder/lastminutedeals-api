"""
generate_demo_video.py — Auto-generate a shareable demo video of the LMD MCP server.

Creates a .webm video showing Claude using our MCP tools to find and book
a last-minute tour. Playable on YouTube, Twitter, Reddit, and all browsers.

Output: .tmp/lmd_demo.webm  (also saves .tmp/lmd_demo_preview.png)

Usage:
    python tools/generate_demo_video.py
    python tools/generate_demo_video.py --slow   # slower typing for readability
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / "tools"))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# ── Load a real slot for the demo ─────────────────────────────────────────────

def _pick_demo_slot() -> dict:
    """Return a clean demo-friendly slot from live data, or a convincing fake."""
    agg = BASE_DIR / ".tmp" / "aggregated_slots.json"
    if agg.exists():
        try:
            slots = json.loads(agg.read_text(encoding="utf-8"))
            # Prefer experience slots with a price
            for s in slots:
                if (s.get("category") == "experiences"
                        and s.get("price")
                        and s.get("service_name")
                        and 2 <= (s.get("hours_until_start") or 0) <= 48):
                    return s
            if slots:
                return slots[0]
        except Exception:
            pass

    # Fallback synthetic slot
    start = datetime.now(timezone.utc) + timedelta(hours=6)
    return {
        "slot_id": "abc123xyz",
        "service_name": "Rome E-Bike City Highlights Tour",
        "business_name": "Bicycle Roma",
        "category": "experiences",
        "location_city": "Rome",
        "location_country": "IT",
        "start_time": start.isoformat(),
        "hours_until_start": 6.0,
        "price": 49.0,
        "our_price": 54.0,
        "currency": "USD",
        "spots_open": 4,
    }


SLOT = _pick_demo_slot()
SLOT_ID   = SLOT.get("slot_id", "abc123")
SVC_NAME  = SLOT.get("service_name", "Tour")
CITY      = SLOT.get("location_city") or "Rome"
HOURS     = round(SLOT.get("hours_until_start") or 6, 1)
PRICE     = SLOT.get("our_price") or SLOT.get("price") or 54
CURRENCY  = SLOT.get("currency", "USD")
SPOTS     = SLOT.get("spots_open") or "—"

try:
    st = SLOT.get("start_time", "")
    if st.endswith("Z"):
        st = st[:-1] + "+00:00"
    dt = datetime.fromisoformat(st)
    START_DISPLAY = dt.strftime("%A, %b %-d at %-I:%M %p UTC")
except Exception:
    START_DISPLAY = "Today at 6:00 PM UTC"

# ── HTML demo page ─────────────────────────────────────────────────────────────

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Last Minute Deals — MCP Demo</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: #0f172a;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    padding: 32px;
  }}

  .window {{
    width: 860px;
    background: #1e293b;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 32px 80px rgba(0,0,0,0.6);
    border: 1px solid #334155;
  }}

  /* Title bar */
  .titlebar {{
    background: #0f172a;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    border-bottom: 1px solid #334155;
  }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .dot-red {{ background: #ef4444; }}
  .dot-yellow {{ background: #f59e0b; }}
  .dot-green {{ background: #22c55e; }}
  .title-text {{
    margin-left: 12px;
    color: #94a3b8;
    font-size: 13px;
    font-weight: 500;
  }}
  .badge {{
    margin-left: auto;
    background: #0ea5e9;
    color: white;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 20px;
    letter-spacing: 0.5px;
  }}

  /* Chat area */
  .chat {{
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    min-height: 520px;
  }}

  .msg {{ display: flex; gap: 12px; align-items: flex-start; }}
  .msg.user {{ flex-direction: row-reverse; }}

  .avatar {{
    width: 36px; height: 36px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; flex-shrink: 0; font-weight: 700;
  }}
  .avatar-claude {{ background: #7c3aed; color: white; font-size: 12px; }}
  .avatar-user  {{ background: #0ea5e9; color: white; }}

  .bubble {{
    max-width: 580px;
    padding: 12px 16px;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.6;
  }}
  .bubble-claude {{
    background: #0f172a;
    color: #e2e8f0;
    border-top-left-radius: 4px;
  }}
  .bubble-user {{
    background: #0ea5e9;
    color: white;
    border-top-right-radius: 4px;
  }}

  /* Tool call block */
  .tool-call {{
    background: #0f172a;
    border: 1px solid #334155;
    border-left: 3px solid #7c3aed;
    border-radius: 8px;
    padding: 12px 16px;
    font-family: 'Cascadia Code', 'Fira Code', monospace;
    font-size: 12px;
    color: #94a3b8;
    max-width: 580px;
    margin-left: 48px;
  }}
  .tool-call .fn-name {{ color: #a78bfa; font-weight: 700; }}
  .tool-call .key {{ color: #38bdf8; }}
  .tool-call .val {{ color: #86efac; }}
  .tool-call .label {{
    font-size: 10px;
    font-weight: 700;
    color: #7c3aed;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 6px;
  }}

  /* Result card */
  .result-card {{
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 16px;
    max-width: 580px;
    margin-left: 48px;
  }}
  .result-card .card-title {{
    font-size: 15px;
    font-weight: 700;
    color: #f1f5f9;
    margin-bottom: 10px;
  }}
  .result-row {{
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    padding: 5px 0;
    border-bottom: 1px solid #1e293b;
  }}
  .result-row:last-child {{ border-bottom: none; }}
  .result-label {{ color: #64748b; }}
  .result-val   {{ color: #e2e8f0; font-weight: 500; }}
  .pill {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
  }}
  .pill-green {{ background: #064e3b; color: #34d399; }}
  .pill-blue  {{ background: #0c4a6e; color: #38bdf8; }}

  /* Checkout link */
  .checkout-link {{
    display: inline-block;
    background: #0ea5e9;
    color: white;
    text-decoration: none;
    padding: 10px 20px;
    border-radius: 8px;
    font-weight: 700;
    font-size: 13px;
    margin-top: 10px;
  }}

  /* Watermark */
  .watermark {{
    padding: 12px 24px;
    background: #0f172a;
    border-top: 1px solid #1e293b;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    color: #475569;
  }}
  .watermark a {{ color: #38bdf8; text-decoration: none; }}

  /* Animations */
  .fade-in {{
    opacity: 0;
    transform: translateY(8px);
    animation: fadeIn 0.5s ease forwards;
  }}
  @keyframes fadeIn {{
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  .cursor {{
    display: inline-block;
    width: 2px;
    height: 1em;
    background: #e2e8f0;
    margin-left: 2px;
    vertical-align: text-bottom;
    animation: blink 0.8s step-start infinite;
  }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}

  /* Hide initially */
  [data-step] {{ display: none; }}
</style>
</head>
<body>
<div class="window">
  <div class="titlebar">
    <div class="dot dot-red"></div>
    <div class="dot dot-yellow"></div>
    <div class="dot dot-green"></div>
    <span class="title-text">Claude — Travel Agent</span>
    <span class="badge">MCP: lastminutedeals</span>
  </div>

  <div class="chat" id="chat">

    <!-- Step 0: User message -->
    <div class="msg user fade-in" data-step="0">
      <div class="avatar avatar-user">J</div>
      <div class="bubble bubble-user">
        Find me a last-minute {SLOT.get('category', 'experience')} available today or tomorrow{f" in {CITY}" if CITY else ""}. Something with instant confirmation.
      </div>
    </div>

    <!-- Step 1: Claude thinks → tool call -->
    <div class="tool-call fade-in" data-step="1">
      <div class="label">🔧 MCP Tool Call</div>
      <span class="fn-name">search_slots</span>(<br>
      &nbsp;&nbsp;<span class="key">city</span>=<span class="val">"{CITY}"</span>,<br>
      &nbsp;&nbsp;<span class="key">category</span>=<span class="val">"{SLOT.get('category', 'experiences')}"</span>,<br>
      &nbsp;&nbsp;<span class="key">hours_ahead</span>=<span class="val">48</span><br>
      )
    </div>

    <!-- Step 2: Result -->
    <div class="result-card fade-in" data-step="2">
      <div class="card-title">📍 {SVC_NAME}</div>
      <div class="result-row">
        <span class="result-label">Starts</span>
        <span class="result-val">{START_DISPLAY} ({HOURS}h away)</span>
      </div>
      <div class="result-row">
        <span class="result-label">Price</span>
        <span class="result-val">${PRICE:.0f} {CURRENCY}</span>
      </div>
      <div class="result-row">
        <span class="result-label">Spots left</span>
        <span class="result-val">{SPOTS}</span>
      </div>
      <div class="result-row">
        <span class="result-label">Confirmation</span>
        <span class="result-val"><span class="pill pill-green">Instant ✓</span></span>
      </div>
      <div class="result-row">
        <span class="result-label">slot_id</span>
        <span class="result-val" style="font-family:monospace;font-size:11px;color:#64748b">{SLOT_ID[:24]}...</span>
      </div>
    </div>

    <!-- Step 3: Claude response -->
    <div class="msg fade-in" data-step="3">
      <div class="avatar avatar-claude">AI</div>
      <div class="bubble bubble-claude">
        Found one with instant confirmation — <strong>{SVC_NAME}</strong> starting in {HOURS} hours at ${PRICE:.0f}.
        Only {SPOTS} spots left. Want me to book it? I'll need your name, email, and phone.
      </div>
    </div>

    <!-- Step 4: User confirms -->
    <div class="msg user fade-in" data-step="4">
      <div class="avatar avatar-user">J</div>
      <div class="bubble bubble-user">
        Yes — book it for Jane Smith, jane@example.com, +1 555 010 2030
      </div>
    </div>

    <!-- Step 5: book_slot tool call -->
    <div class="tool-call fade-in" data-step="5">
      <div class="label">🔧 MCP Tool Call</div>
      <span class="fn-name">book_slot</span>(<br>
      &nbsp;&nbsp;<span class="key">slot_id</span>=<span class="val">"{SLOT_ID[:20]}..."</span>,<br>
      &nbsp;&nbsp;<span class="key">customer_name</span>=<span class="val">"Jane Smith"</span>,<br>
      &nbsp;&nbsp;<span class="key">customer_email</span>=<span class="val">"jane@example.com"</span>,<br>
      &nbsp;&nbsp;<span class="key">customer_phone</span>=<span class="val">"+15550102030"</span><br>
      )
    </div>

    <!-- Step 6: Booking result -->
    <div class="result-card fade-in" data-step="6">
      <div class="card-title">✅ Booking Created</div>
      <div class="result-row">
        <span class="result-label">booking_id</span>
        <span class="result-val" style="font-family:monospace;font-size:11px">bk_a3f9c12e</span>
      </div>
      <div class="result-row">
        <span class="result-label">Status</span>
        <span class="result-val"><span class="pill pill-blue">Awaiting payment</span></span>
      </div>
      <div class="result-row">
        <span class="result-label">Next step</span>
        <span class="result-val">Stripe checkout → confirm on supplier</span>
      </div>
      <a class="checkout-link" href="#">Complete Payment →</a>
    </div>

    <!-- Step 7: Claude wraps up -->
    <div class="msg fade-in" data-step="7">
      <div class="avatar avatar-claude">AI</div>
      <div class="bubble bubble-claude">
        Done! Booking created for Jane. She'll get a confirmation email once payment clears.
        The confirmation number and supplier details will be sent automatically.<span class="cursor"></span>
      </div>
    </div>

  </div>

  <div class="watermark">
    <span>lastminutedeals MCP server — <a href="https://lastminutedealshq.com">lastminutedealshq.com</a></span>
    <span>search_slots · book_slot · get_booking_status · refresh_slots</span>
  </div>
</div>

<script>
const steps = document.querySelectorAll('[data-step]');
const delays = [400, 1200, 2000, 3200, 5000, 6000, 7200, 9000];

steps.forEach((el, i) => {{
  setTimeout(() => {{
    el.style.display = '';
    el.style.animationDelay = '0s';
    // scroll into view
    el.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
  }}, delays[i] || i * 1200);
}});
</script>
</body>
</html>
"""

# ── Playwright recording ───────────────────────────────────────────────────────

def generate_video(slow: bool = False) -> Path:
    from playwright.sync_api import sync_playwright

    output_dir  = BASE_DIR / ".tmp"
    video_path  = output_dir / "lmd_demo.webm"
    preview_path = output_dir / "lmd_demo_preview.png"
    html_path   = output_dir / "_demo_page.html"

    html_path.write_text(HTML, encoding="utf-8")
    print(f"[DEMO] Demo page written to {html_path}")

    total_ms = 12000 if not slow else 18000  # total animation duration

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 960, "height": 700},
            record_video_dir=str(output_dir),
            record_video_size={"width": 960, "height": 700},
        )
        page = context.new_page()
        page.goto(f"file:///{html_path.as_posix()}")
        print(f"[DEMO] Recording — waiting {total_ms // 1000}s for animation...")
        page.wait_for_timeout(total_ms)

        # Screenshot for preview/thumbnail
        page.screenshot(path=str(preview_path))
        print(f"[DEMO] Preview saved: {preview_path}")

        context.close()
        browser.close()

    # Playwright names the video file with a UUID — find and rename it
    import glob, shutil
    vids = sorted(glob.glob(str(output_dir / "*.webm")), key=os.path.getmtime, reverse=True)
    if vids:
        latest = Path(vids[0])
        if latest != video_path:
            shutil.move(str(latest), str(video_path))
        print(f"[DEMO] OK Video saved: {video_path}  ({video_path.stat().st_size // 1024} KB)")
    else:
        print("[DEMO] Warning: no .webm file found after recording")

    return video_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow", action="store_true", help="Slower animation for readability")
    args = parser.parse_args()

    vid = generate_video(slow=args.slow)
    print(f"\nShare this file:\n  {vid}\n")
    print("Upload directly to YouTube, Twitter, Reddit, or LinkedIn.")
    print("Open in any browser to preview locally.")
