"""
telegram_bot.py — Telegram bot for Last Minute Tour Finder.

Users send a city name, get back available tours with booking links.
Runs as a long-polling bot — no webhook setup needed.

Usage:
    python tools/telegram_bot.py
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE = "https://api.lastminutedealshq.com"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Telegram API helpers ─────────────────────────────────────────────────────

def tg_request(method, data=None):
    url = f"{TELEGRAM_API}/{method}"
    if data:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  TG API error {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        print(f"  TG request failed: {e}")
        return None


def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_request("sendMessage", data)


# ── Tour search ──────────────────────────────────────────────────────────────

def search_tours(city, limit=5):
    params = urllib.parse.urlencode({"city": city, "hours_ahead": 72, "limit": limit})
    url = f"{API_BASE}/slots?{params}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "slots" in data:
                return data["slots"]
            return []
    except Exception as e:
        print(f"  API search error: {e}")
        return []


def format_tour(slot):
    name = slot.get("service_name", slot.get("name", "Tour"))
    operator = slot.get("business_name", slot.get("supplier", ""))
    price = slot.get("price", "")
    currency = slot.get("currency", "USD")
    city = slot.get("location_city", slot.get("city", ""))
    start = slot.get("start_time", "")
    duration = slot.get("duration_minutes", "")
    slot_id = slot.get("slot_id", slot.get("id", ""))
    hours_until = slot.get("hours_until_start", "")
    open_spots = slot.get("open_spots", slot.get("vacancies", ""))

    # Format start time
    time_str = ""
    if start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            time_str = dt.strftime("%a %b %d at %I:%M %p")
        except Exception:
            time_str = start[:16]

    lines = [f"<b>{name}</b>"]
    if operator:
        lines.append(f"by {operator}")
    if price:
        lines.append(f"💰 {currency} {price}")
    if time_str:
        lines.append(f"📅 {time_str}")
    if duration:
        lines.append(f"⏱ {duration} min")
    if open_spots:
        lines.append(f"👥 {open_spots} spots left")
    if hours_until:
        lines.append(f"⏰ Starts in {int(float(hours_until))}h")

    if slot_id:
        book_url = f"{API_BASE}/book/{slot_id}"
        lines.append(f'\n<a href="{book_url}">📋 Book now</a>')

    return "\n".join(lines)


# ── Known cities for suggestions ─────────────────────────────────────────────

TOP_CITIES = [
    "Rome", "Istanbul", "Reykjavik", "Paris", "Cairo", "Barcelona",
    "Amsterdam", "Lisbon", "Kyoto", "Marrakech", "London", "Antalya",
    "Porto", "Bucharest", "Helsinki", "Kotor", "Washington DC",
]


# ── Message handlers ─────────────────────────────────────────────────────────

def handle_start(chat_id):
    text = (
        "👋 <b>Welcome to Last Minute Tour Finder!</b>\n\n"
        "I help you find and book tours and activities around the world — "
        "departing in the next 72 hours.\n\n"
        "<b>How to use:</b>\n"
        "Just type a city name, like <code>Rome</code> or <code>Istanbul</code>\n\n"
        "<b>Popular cities:</b>\n"
        "Rome • Istanbul • Reykjavik • Paris • Cairo • Barcelona • "
        "Amsterdam • Lisbon • London • Marrakech • Kyoto\n\n"
        "37 local operators across 100+ cities. Real inventory, "
        "instant Stripe checkout, confirmed with the operator on the spot."
    )
    send_message(chat_id, text)


def handle_help(chat_id):
    text = (
        "<b>Commands:</b>\n"
        "/start — Welcome message\n"
        "/help — This help text\n\n"
        "<b>Search:</b>\n"
        "Just type a city name! Examples:\n"
        "<code>Rome</code>\n"
        "<code>Istanbul</code>\n"
        "<code>Reykjavik</code>\n"
        "<code>Paris under 50</code>\n\n"
        "I'll show you what's available in the next 72 hours "
        "with direct booking links."
    )
    send_message(chat_id, text)


def handle_search(chat_id, text):
    # Parse optional price filter: "rome under 50"
    parts = text.strip().split()
    max_price = None
    city_parts = []
    i = 0
    while i < len(parts):
        if parts[i].lower() == "under" and i + 1 < len(parts):
            try:
                max_price = float(parts[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        city_parts.append(parts[i])
        i += 1

    city = " ".join(city_parts).strip()
    if not city:
        send_message(chat_id, "Please type a city name, like <code>Rome</code> or <code>Istanbul</code>")
        return

    send_message(chat_id, f"🔍 Searching tours in <b>{city}</b>...")

    tours = search_tours(city, limit=5)

    if not tours:
        nearby = ", ".join(TOP_CITIES[:6])
        send_message(
            chat_id,
            f"No tours found in <b>{city}</b> for the next 72 hours.\n\n"
            f"Try one of these: {nearby}"
        )
        return

    # Filter by price if specified
    if max_price:
        tours = [t for t in tours if float(t.get("price", 9999)) <= max_price]
        if not tours:
            send_message(
                chat_id,
                f"No tours under {max_price} found in <b>{city}</b>. "
                "Try without a price limit."
            )
            return

    header = f"🎯 <b>{len(tours)} tour{'s' if len(tours) != 1 else ''} in {city}</b>\n"
    msg_parts = [header]
    for tour in tours[:5]:
        msg_parts.append(format_tour(tour))

    msg_parts.append(
        "\n💡 <i>Click 'Book now' to see details, enter your info, "
        "and pay securely via Stripe. Instant confirmation.</i>"
    )

    full_msg = "\n\n".join(msg_parts)
    # Telegram message limit is 4096 chars
    if len(full_msg) > 4000:
        full_msg = full_msg[:3990] + "..."

    send_message(chat_id, full_msg, parse_mode="HTML")


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    # Verify bot
    me = tg_request("getMe")
    if not me or not me.get("ok"):
        print("ERROR: Could not connect to Telegram bot")
        sys.exit(1)

    bot_name = me["result"]["username"]
    print(f"Bot @{bot_name} is running. Polling for messages...")

    offset = 0
    while True:
        try:
            updates = tg_request("getUpdates", {"offset": offset, "timeout": 30})
            if not updates or not updates.get("ok"):
                time.sleep(5)
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg or not msg.get("text"):
                    continue

                chat_id = msg["chat"]["id"]
                text = msg["text"].strip()
                user = msg.get("from", {})
                username = user.get("username", user.get("first_name", "unknown"))

                print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"@{username}: {text}")

                if text.startswith("/start"):
                    handle_start(chat_id)
                elif text.startswith("/help"):
                    handle_help(chat_id)
                else:
                    # Treat any text as a city search
                    handle_search(chat_id, text)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print(f"  Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
