"""
generate_deal_visual.py — Auto-generate deal announcement images for Instagram/TikTok.

Creates a 1080x1080 deal card image (Instagram square format) and a
1080x1920 story/reel format for each slot. Uses Pillow — no external
services required.

Design:
  - Dark background with a category-specific accent color
  - Bold service name, location, time, and price
  - "LastMinuteDeals" branding in the bottom corner
  - Urgency badge ("LAST CHANCE", "TODAY", "THIS WEEK")

Usage:
    python tools/generate_deal_visual.py --slot-id <id>
    python tools/generate_deal_visual.py --top-n 10
    python tools/generate_deal_visual.py --top-n 5 --format story

Outputs to .tmp/visuals/<slot_id>_square.png and <slot_id>_story.png
"""

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

DATA_FILE   = Path(".tmp/aggregated_slots.json")
OUTPUT_DIR  = Path(".tmp/visuals")

# Category accent colors (RGB)
CAT_COLORS = {
    "events":                (220, 38,  38),   # red
    "wellness":              (5,   150, 105),  # emerald
    "beauty":                (217, 70,  239),  # purple/pink
    "hospitality":           (37,  99,  235),  # blue
    "home_services":         (245, 158, 11),   # amber
    "professional_services": (99,  102, 241),  # indigo
}

CAT_LABEL = {
    "events":                "Event",
    "wellness":              "Wellness",
    "beauty":                "Beauty",
    "hospitality":           "Stay",
    "home_services":         "Home Service",
    "professional_services": "Professional",
}

CAT_ICON_CHAR = {
    "events":                "EVENT",
    "wellness":              "WELLNESS",
    "beauty":                "BEAUTY",
    "hospitality":           "STAY",
    "home_services":         "HOME",
    "professional_services": "PRO",
}

BG_COLOR   = (15, 23, 42)      # dark navy (#0f172a)
TEXT_WHITE = (255, 255, 255)
TEXT_MUTED = (148, 163, 184)   # slate-400
BRAND_COLOR= (99, 179, 237)    # sky blue for branding


def _load_font(size: int, bold: bool = False):
    """Load a system font, falling back to default."""
    font_candidates = [
        "arialbd.ttf"   if bold else "arial.ttf",
        "Arial Bold.ttf"if bold else "Arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf",
    ]
    for name in font_candidates:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, max_chars: int) -> list[str]:
    return textwrap.wrap(text, width=max_chars) or [""]


def generate_square(slot: dict) -> Path:
    """Generate a 1080x1080 square deal card."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    W, H = 1080, 1080
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    cat    = slot.get("category", "events")
    accent = CAT_COLORS.get(cat, (99, 102, 241))
    label  = CAT_LABEL.get(cat, cat.title())
    badge  = CAT_ICON_CHAR.get(cat, cat.upper()[:6])

    # ── Top accent bar ─────────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (W, 8)], fill=accent)

    # ── Category badge ─────────────────────────────────────────────────────────
    badge_font = _load_font(28, bold=True)
    badge_w    = draw.textlength(badge, font=badge_font)
    pad        = 16
    badge_box  = [(50, 40), (50 + badge_w + pad * 2, 40 + 44)]
    draw.rounded_rectangle(badge_box, radius=8, fill=accent)
    draw.text((50 + pad, 44), badge, font=badge_font, fill=TEXT_WHITE)

    # ── Urgency badge (top right) ──────────────────────────────────────────────
    hours = slot.get("hours_until_start")
    if hours is not None:
        if hours <= 6:
            urgency_text  = "LAST CHANCE"
            urgency_color = (220, 38, 38)
        elif hours <= 12:
            urgency_text  = "TODAY"
            urgency_color = (245, 158, 11)
        elif hours <= 24:
            urgency_text  = "TOMORROW"
            urgency_color = (37, 99, 235)
        else:
            urgency_text  = f"{hours:.0f}H AWAY"
            urgency_color = (71, 85, 105)

        u_font = _load_font(26, bold=True)
        u_w    = draw.textlength(urgency_text, font=u_font)
        u_pad  = 14
        u_box  = [(W - 50 - u_w - u_pad * 2, 40), (W - 50, 40 + 40)]
        draw.rounded_rectangle(u_box, radius=8, fill=urgency_color)
        draw.text((W - 50 - u_w - u_pad, 44), urgency_text, font=u_font, fill=TEXT_WHITE)

    # ── Service name ───────────────────────────────────────────────────────────
    service = slot.get("service_name", "Last-Minute Deal")
    name_font = _load_font(72, bold=True)
    lines     = _wrap_text(service, 22)
    y = 160
    for line in lines[:3]:
        draw.text((50, y), line, font=name_font, fill=TEXT_WHITE)
        y += 84

    # ── Business name ──────────────────────────────────────────────────────────
    biz = slot.get("business_name", "")
    if biz:
        biz_font = _load_font(42)
        draw.text((50, y + 10), biz, font=biz_font, fill=TEXT_MUTED)
        y += 60

    # ── Divider ────────────────────────────────────────────────────────────────
    y += 30
    draw.line([(50, y), (W - 50, y)], fill=(51, 65, 85), width=2)
    y += 40

    # ── Location ───────────────────────────────────────────────────────────────
    city  = slot.get("location_city", "")
    state = slot.get("location_state", "")
    loc_str   = f"  {city}, {state}" if city and state else f"  {city or state}"
    loc_font  = _load_font(44)
    draw.text((50, y), loc_str, font=loc_font, fill=TEXT_MUTED)
    y += 60

    # ── Date / time ────────────────────────────────────────────────────────────
    start_str = ""
    try:
        start_dt  = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00"))
        start_str = "  " + start_dt.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ").replace("  ", " ")
    except Exception:
        pass
    if start_str:
        draw.text((50, y), start_str, font=loc_font, fill=TEXT_MUTED)
        y += 60

    # ── Price ──────────────────────────────────────────────────────────────────
    price     = slot.get("our_price") or slot.get("price")
    y += 20
    if price is not None and float(price) > 0:
        price_font = _load_font(96, bold=True)
        price_str  = f"${float(price):.0f}"
        draw.text((50, y), price_str, font=price_font, fill=accent)
        draw.text((50 + draw.textlength(price_str, font=price_font) + 12, y + 52),
                  "/ person", font=_load_font(38), fill=TEXT_MUTED)
    elif price == 0:
        draw.text((50, y), "FREE", font=_load_font(96, bold=True), fill=accent)

    # ── Bottom branding ────────────────────────────────────────────────────────
    brand_font = _load_font(34, bold=True)
    brand_text = "LastMinuteDeals.com"
    b_w        = draw.textlength(brand_text, font=brand_font)
    draw.text((W - 50 - b_w, H - 60), brand_text, font=brand_font, fill=BRAND_COLOR)

    # ── Bottom accent bar ──────────────────────────────────────────────────────
    draw.rectangle([(0, H - 6), (W, H)], fill=accent)

    out_path = OUTPUT_DIR / f"{slot['slot_id'][:16]}_square.png"
    img.save(str(out_path), "PNG")
    return out_path


def generate_story(slot: dict) -> Path:
    """Generate a 1080x1920 story/reel format image."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    W, H = 1080, 1920
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    cat    = slot.get("category", "events")
    accent = CAT_COLORS.get(cat, (99, 102, 241))
    badge  = CAT_ICON_CHAR.get(cat, cat.upper()[:6])

    # ── Top gradient-like blocks ───────────────────────────────────────────────
    draw.rectangle([(0, 0), (W, 12)], fill=accent)
    draw.rectangle([(0, 12), (W, 200)], fill=tuple(max(0, c - 60) for c in accent))

    # ── Category badge ─────────────────────────────────────────────────────────
    badge_font = _load_font(34, bold=True)
    badge_w    = draw.textlength(badge, font=badge_font)
    pad        = 18
    badge_box  = [(W//2 - badge_w//2 - pad, 40), (W//2 + badge_w//2 + pad, 40 + 54)]
    draw.rounded_rectangle(badge_box, radius=10, fill=accent)
    draw.text((W//2 - badge_w//2, 46), badge, font=badge_font, fill=TEXT_WHITE)

    # ── Main content centered ──────────────────────────────────────────────────
    y = 260

    # Urgency
    hours = slot.get("hours_until_start")
    if hours is not None:
        urgency_text = (
            "LAST CHANCE" if hours <= 6 else
            f"TODAY - {hours:.0f}h away" if hours <= 12 else
            f"TOMORROW" if hours <= 24 else
            f"{hours:.0f} HOURS AWAY"
        )
        urg_font = _load_font(40, bold=True)
        urg_w    = draw.textlength(urgency_text, font=urg_font)
        draw.text((W//2 - urg_w//2, y), urgency_text, font=urg_font, fill=TEXT_MUTED)
        y += 70

    # Service name
    service    = slot.get("service_name", "Last-Minute Deal")
    name_font  = _load_font(80, bold=True)
    lines      = _wrap_text(service, 18)
    for line in lines[:3]:
        lw = draw.textlength(line, font=name_font)
        draw.text((W//2 - lw//2, y), line, font=name_font, fill=TEXT_WHITE)
        y += 96
    y += 20

    # Business
    biz = slot.get("business_name", "")
    if biz:
        biz_font = _load_font(48)
        biz_w    = draw.textlength(biz[:40], font=biz_font)
        draw.text((W//2 - biz_w//2, y), biz[:40], font=biz_font, fill=TEXT_MUTED)
        y += 72

    # Divider
    y += 30
    draw.line([(W//4, y), (3*W//4, y)], fill=(51, 65, 85), width=2)
    y += 50

    # Location
    city  = slot.get("location_city", "")
    state = slot.get("location_state", "")
    loc_str = f"{city}, {state}" if city and state else city or state
    loc_font = _load_font(50)
    loc_w    = draw.textlength(loc_str, font=loc_font)
    draw.text((W//2 - loc_w//2, y), loc_str, font=loc_font, fill=TEXT_MUTED)
    y += 72

    # Date
    try:
        start_dt  = datetime.fromisoformat(slot["start_time"].replace("Z", "+00:00"))
        date_str  = start_dt.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ")
        date_w    = draw.textlength(date_str, font=loc_font)
        draw.text((W//2 - date_w//2, y), date_str, font=loc_font, fill=TEXT_MUTED)
        y += 72
    except Exception:
        pass

    # Price — large, centered
    price = slot.get("our_price") or slot.get("price")
    y += 40
    if price is not None and float(price) > 0:
        price_font = _load_font(160, bold=True)
        price_str  = f"${float(price):.0f}"
        p_w        = draw.textlength(price_str, font=price_font)
        draw.text((W//2 - p_w//2, y), price_str, font=price_font, fill=accent)
        y += 180
        pp_font = _load_font(44)
        pp_w    = draw.textlength("per person", font=pp_font)
        draw.text((W//2 - pp_w//2, y), "per person", font=pp_font, fill=TEXT_MUTED)
        y += 70
    elif price == 0:
        free_font = _load_font(160, bold=True)
        free_w    = draw.textlength("FREE", font=free_font)
        draw.text((W//2 - free_w//2, y), "FREE", font=free_font, fill=accent)
        y += 200

    # CTA
    y += 40
    cta_font = _load_font(52, bold=True)
    cta_text = "Book Now at LastMinuteDeals.com"
    cta_w    = draw.textlength(cta_text, font=cta_font)
    # CTA pill
    cta_pad = 28
    cta_box = [
        (W//2 - cta_w//2 - cta_pad, y - 8),
        (W//2 + cta_w//2 + cta_pad, y + 68),
    ]
    draw.rounded_rectangle(cta_box, radius=16, fill=accent)
    draw.text((W//2 - cta_w//2, y), cta_text, font=cta_font, fill=TEXT_WHITE)

    # Bottom branding
    brand_font = _load_font(36, bold=True)
    brand_text = "LastMinuteDeals"
    b_w        = draw.textlength(brand_text, font=brand_font)
    draw.text((W//2 - b_w//2, H - 80), brand_text, font=brand_font, fill=BRAND_COLOR)
    draw.rectangle([(0, H - 6), (W, H)], fill=accent)

    out_path = OUTPUT_DIR / f"{slot['slot_id'][:16]}_story.png"
    img.save(str(out_path), "PNG")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate deal announcement images")
    parser.add_argument("--slot-id",  help="Generate visuals for a specific slot_id")
    parser.add_argument("--top-n",    type=int, default=5,
                        help="Generate visuals for top N most urgent priced slots (default: 5)")
    parser.add_argument("--format",   choices=["square", "story", "both"], default="both",
                        help="Image format to generate (default: both)")
    parser.add_argument("--category", help="Filter by category")
    args = parser.parse_args()

    if not DATA_FILE.exists():
        print(f"No slot data at {DATA_FILE}. Run the pipeline first.")
        sys.exit(1)

    slots = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    if args.slot_id:
        slot = next((s for s in slots if s.get("slot_id") == args.slot_id), None)
        if not slot:
            print(f"Slot not found: {args.slot_id}")
            sys.exit(1)
        targets = [slot]
    else:
        # Select top N most urgent priced slots
        candidates = [
            s for s in slots
            if (s.get("our_price") or s.get("price"))
            and s.get("hours_until_start") is not None
            and s.get("hours_until_start") <= 72
            and (not args.category or s.get("category") == args.category)
        ]
        candidates.sort(key=lambda s: s.get("hours_until_start") or 9999)
        targets = candidates[:args.top_n]

    if not targets:
        print("No matching slots found.")
        sys.exit(0)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating visuals for {len(targets)} slot(s) -> {OUTPUT_DIR}")

    for slot in targets:
        name = slot.get("service_name", "")[:50]
        city = slot.get("location_city", "")

        if args.format in ("square", "both"):
            path = generate_square(slot)
            print(f"  Square: {path.name}  ({name} | {city})")

        if args.format in ("story", "both"):
            path = generate_story(slot)
            print(f"  Story:  {path.name}  ({name} | {city})")

    print(f"\nDone. {len(targets) * (2 if args.format == 'both' else 1)} images saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
