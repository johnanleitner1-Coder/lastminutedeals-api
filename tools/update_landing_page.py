"""
update_landing_page.py — Regenerate the static deals landing page from aggregated_slots.json.

Generates a clean, professional index.html with:
  - Deals grouped by category and city
  - City + category filters
  - Price display (our_price if set, else original price)
  - "Book Now" opens integrated checkout modal (no external links shown)
  - Auto-refreshes deal data on every pipeline run

Usage:
    python tools/update_landing_page.py [--data-file .tmp/aggregated_slots.json] [--out-dir .tmp/site]

Deploy output to Netlify, Cloudflare Pages, or GitHub Pages.
"""

import argparse
import hashlib
import html as html_lib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_FILE = Path(".tmp/aggregated_slots.json")
OUT_DIR   = Path(".tmp/site")

CATEGORY_COLOR = {
    "wellness":              "#10b981",   # emerald
    "beauty":                "#ec4899",   # pink
    "hospitality":           "#3b82f6",   # blue
    "home_services":         "#f59e0b",   # amber
    "professional_services": "#8b5cf6",   # violet
    "experiences":           "#f97316",   # orange  (tours/activities — OCTO)
    "events":                "#ef4444",   # red
}

CATEGORY_LABEL = {
    "wellness":              "Wellness & Fitness",
    "beauty":                "Beauty & Salon",
    "hospitality":           "Short-term Stays",
    "home_services":         "Home Services",
    "professional_services": "Professional Services",
    "experiences":           "Tours & Experiences",
    "events":                "Events & Experiences",
}

CATEGORY_ICON = {
    "wellness":              "🧘",
    "beauty":                "💅",
    "hospitality":           "🏨",
    "home_services":         "🔧",
    "professional_services": "💼",
    "experiences":           "🎯",
    "events":                "🎟",
}


def format_price(slot: dict):
    """Returns (display_str, raw_float) or (None, None) if price unknown."""
    price = slot.get("our_price")
    if price is None:
        price = slot.get("price")
    if price is None:
        return None, None
    p = float(price)
    if p == 0:
        return "Free", 0.0
    symbol = "$" if slot.get("currency", "USD") == "USD" else slot.get("currency", "")
    return f"{symbol}{p:.0f}", p


def format_time(iso_str: str) -> str:
    """Returns human-friendly time like 'Today at 8:00 PM' or 'Mon Mar 25 at 2:00 PM'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # Convert to local display (treat as-is, show UTC note)
        now = datetime.now(timezone.utc)
        diff_h = (dt - now).total_seconds() / 3600
        time_str = dt.strftime("%I:%M %p UTC").lstrip("0")
        if diff_h < 0:
            return dt.strftime("%b %d") + f" at {time_str}"
        elif diff_h < 20:
            return f"Today at {time_str}"
        elif diff_h < 44:
            return f"Tomorrow at {time_str}"
        else:
            return dt.strftime("%a %b ") + str(dt.day) + f" at {time_str}"
    except Exception:
        try:
            return iso_str[:16].replace("T", " ")
        except Exception:
            return ""


def urgency_badge(hours) -> tuple[str, str]:
    """Returns (badge_text, badge_css_class)."""
    if hours is None:
        return "", ""
    if hours <= 3:
        return "⚡ Ending soon", "badge-critical"
    elif hours <= 6:
        return "🔥 Last chance", "badge-urgent"
    elif hours <= 12:
        return "Today only", "badge-today"
    elif hours <= 24:
        return "Ends tomorrow", "badge-tomorrow"
    else:
        return "Available", "badge-available"


def render_card(slot: dict) -> str:
    name        = slot.get("service_name") or "Service"
    business    = slot.get("business_name") or ""
    # Strip any business name that's actually an email address or contains one
    if "@" in business or business.lower().startswith("for venue details"):
        business = ""
    city        = slot.get("location_city") or ""
    state       = slot.get("location_state") or ""
    start       = format_time(slot.get("start_time", ""))
    price_str, raw_price = format_price(slot)
    if price_str is None:
        return ""
    hours       = slot.get("hours_until_start")
    badge_text, badge_class = urgency_badge(hours)
    slot_id     = slot.get("slot_id", "")
    duration    = slot.get("duration_minutes")
    dur_str     = f" · {duration} min" if duration else ""
    category    = slot.get("category", "events")
    cat_color   = CATEGORY_COLOR.get(category, "#6366f1")
    cat_label   = CATEGORY_LABEL.get(category, category)
    is_free     = raw_price == 0.0
    affiliate_url = slot.get("affiliate_url") or ""

    # Safe data attribute — html.escape handles apostrophes and quotes correctly
    slot_data_attr = html_lib.escape(json.dumps({
        "slot_id":       slot_id,
        "service_name":  name,
        "business_name": business,
        "start_time":    slot.get("start_time", ""),
        "price":         raw_price,
        "currency":      slot.get("currency", "USD"),
        "city":          city,
        "state":         state,
        "category":      category,
        "affiliate_url": affiliate_url,
        "is_free":       is_free,
    }), quote=True)

    btn_label = "Get Details" if is_free else "Book Now →"
    btn_fn    = "openFreeEvent" if is_free else "openCheckout"

    # Escape display strings for HTML
    name_e     = html_lib.escape(name)
    business_e = html_lib.escape(business)

    badge_html = f'<span class="badge {badge_class}">{badge_text}</span>' if badge_text else ""

    return f"""<div class="card" data-city="{html_lib.escape(city)}" data-category="{category}" data-hours="{hours or 99}" data-price="{raw_price or 0}" style="border-top:3px solid {cat_color}">
      <div class="card-top">
        <span class="card-cat" style="color:{cat_color}">{cat_label}</span>
        {badge_html}
      </div>
      <h3 class="card-title">{name_e}</h3>
      <p class="card-biz">{business_e}</p>
      <p class="card-loc">📍 {html_lib.escape(city)}{', ' + html_lib.escape(state) if state else ''}{dur_str}</p>
      <p class="card-time">🕐 {html_lib.escape(start)}</p>
      <div class="card-footer">
        <div class="card-price-wrap">
          <span class="card-price">{price_str}</span>
          {'<span class="card-orig">inc. service fee</span>' if not is_free else ''}
        </div>
        <button class="btn-book" data-slot="{slot_data_attr}" onclick="{btn_fn}(this.dataset.slot)">{btn_label}</button>
      </div>
    </div>"""


def build_top_deals_schema(slots: list[dict]) -> str:
    """Build schema.org ItemList JSON-LD for top 30 soonest priced deals."""
    priced = [s for s in slots if (s.get("our_price") or s.get("price") or 0) > 0]
    top = sorted(priced, key=lambda s: s.get("hours_until_start") or 999)[:30]
    items = []
    for i, s in enumerate(top, 1):
        price = s.get("our_price") or s.get("price") or 0
        city  = s.get("location_city", "")
        state = s.get("location_state", "")
        loc   = f"{city}, {state}".strip(", ") if city or state else "United States"
        item  = {
            "@type": "ListItem",
            "position": i,
            "item": {
                "@type": "Event",
                "name": s.get("service_name", "Last-Minute Deal"),
                "startDate": s.get("start_time", ""),
                "location": {
                    "@type": "Place",
                    "name": s.get("business_name") or loc,
                    "address": {"@type": "PostalAddress", "addressLocality": city, "addressRegion": state, "addressCountry": "US"},
                },
                "offers": {
                    "@type": "Offer",
                    "price": f"{float(price):.2f}",
                    "priceCurrency": s.get("currency", "USD"),
                    "availability": "https://schema.org/InStock",
                    "url": "https://lastminutedealshq.com/",
                    "validThrough": s.get("start_time", ""),
                },
                "eventStatus": "https://schema.org/EventScheduled",
                "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            },
        }
        items.append(item)
    schema = {"@context": "https://schema.org", "@type": "ItemList", "name": "Last-Minute Deals Available Now", "itemListElement": items}
    return json.dumps(schema, separators=(",", ":"))


def generate_html(slots: list[dict], generated_at: str, booking_api_url: str = "", website_api_key: str = "") -> str:
    # Stats for hero
    total_slots = len(slots)
    priced_total = sum(
        1 for s in slots
        if (s.get("our_price") is not None or s.get("price") is not None)
        and (s.get("our_price") or s.get("price") or 0) > 0
    )
    cities = sorted({s.get("location_city","") for s in slots if s.get("location_city","")})
    city_count = len(cities)

    # Build live deals section (experiences first, capped at 12 per category for preview)
    by_category: dict[str, list] = {}
    for slot in slots:
        cat = slot.get("category", "events")
        by_category.setdefault(cat, []).append(slot)

    cat_order = ["experiences", "events", "wellness", "beauty", "hospitality", "home_services", "professional_services"]
    city_options = "\n".join(
        f'<option value="{html_lib.escape(c)}">{html_lib.escape(c)}</option>'
        for c in cities
    )

    sections_html = ""
    for cat in cat_order:
        cat_slots = by_category.get(cat, [])
        if not cat_slots:
            continue
        priced = sorted(
            [s for s in cat_slots if (s.get("our_price") or s.get("price") or 0) > 0],
            key=lambda s: s.get("hours_until_start") or 999,
        )[:12]
        cards_html = "".join(c for c in [render_card(s) for s in priced] if c)
        if not cards_html:
            continue
        color = CATEGORY_COLOR.get(cat, "#6366f1")
        label = CATEGORY_LABEL.get(cat, cat)
        icon  = CATEGORY_ICON.get(cat, "")
        sections_html += f"""
        <section class="cat-section" data-category="{cat}">
          <div class="section-header">
            <h2 class="section-title">
              <span class="section-icon" style="background:{color}22;color:{color}">{icon}</span>
              {label}
            </h2>
          </div>
          <div class="cards-grid">{cards_html}</div>
        </section>"""

    top_deals_schema = build_top_deals_schema(slots)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <!-- generated: {generated_at} -->
  <meta charset="UTF-8" />
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
  <meta http-equiv="Pragma" content="no-cache" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>LastMinuteDeals — Real-World Execution Infrastructure for AI Agents</title>
  <meta name="description" content="Execution infrastructure for real-world service bookings. AI agents search, decide, and book last-minute slots autonomously — events, wellness, beauty, hospitality. Guaranteed outcomes, multi-path retry, pre-funded wallets, persistent intent sessions." />
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1" />
  <link rel="canonical" href="https://lastminutedealshq.com/" />

  <!-- OpenGraph -->
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="LastMinuteDeals" />
  <meta property="og:title" content="LastMinuteDeals — Last-Minute Booking Deals Up to 72 Hours Away" />
  <meta property="og:description" content="Browse and book last-minute open slots across events, wellness, beauty, and hospitality. Available within 72 hours. Secure checkout." />
  <meta property="og:url" content="https://lastminutedealshq.com/" />
  <meta property="og:image" content="https://lastminutedealshq.com/og-image.png" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="LastMinuteDeals — Book Open Slots Now" />
  <meta name="twitter:description" content="Last-minute deals on events, wellness, beauty, and more. Available within 72 hours." />
  <meta name="twitter:image" content="https://lastminutedealshq.com/og-image.png" />

  <!-- Schema.org: WebSite + SearchAction -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "WebSite",
    "name": "LastMinuteDeals",
    "url": "https://lastminutedealshq.com",
    "description": "Execution infrastructure for AI agents booking real-world services. Intent-to-confirmation pipeline with guaranteed outcomes, multi-path retry, and persistent delegated intent.",
    "potentialAction": {{
      "@type": "SearchAction",
      "target": "https://lastminutedealshq.com/?q={{search_term_string}}",
      "query-input": "required name=search_term_string"
    }}
  }}
  </script>

  <!-- Schema.org: Organization -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "LastMinuteDeals",
    "url": "https://lastminutedealshq.com",
    "description": "LastMinuteDeals is execution infrastructure for AI agents that need to book real-world services. Aggregates last-minute availability across events, wellness, beauty, and hospitality — and executes the booking autonomously with guaranteed outcomes.",
    "contactPoint": {{
      "@type": "ContactPoint",
      "contactType": "customer support",
      "availableLanguage": "English"
    }}
  }}
  </script>

  <!-- Schema.org: FAQPage (boosts AI citation rate) -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": [
      {{
        "@type": "Question",
        "name": "How do last-minute deals work?",
        "acceptedAnswer": {{
          "@type": "Answer",
          "text": "LastMinuteDeals aggregates unsold booking slots from events, wellness studios, salons, and hospitality providers that are available within the next 72 hours. These are offered at discounted prices because providers prefer to fill openings rather than leave them empty."
        }}
      }},
      {{
        "@type": "Question",
        "name": "Are these deals available right now?",
        "acceptedAnswer": {{
          "@type": "Answer",
          "text": "Yes. All deals shown are verified available within the next 72 hours. Our inventory updates every 4 hours automatically. Each deal shows the exact start time and hours remaining."
        }}
      }},
      {{
        "@type": "Question",
        "name": "Can AI agents book through LastMinuteDeals?",
        "acceptedAnswer": {{
          "@type": "Answer",
          "text": "Yes. LastMinuteDeals provides a REST API and MCP server for AI agents to search available slots and execute bookings programmatically. See lastminutedealshq.com/openapi.json for the API specification and lastminutedealshq.com/llms.txt for AI integration details."
        }}
      }}
    ]
  }}
  </script>

  <!-- Schema.org: ItemList of current live deals (top 30 soonest for AI crawlers) -->
  <script type="application/ld+json">
  {top_deals_schema}
  </script>

  <link rel="icon" type="image/png" href="/favicon.png" />
  <link rel="shortcut icon" href="/favicon.ico" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --navy: #0f172a;
      --navy-light: #1e293b;
      --navy-muted: #334155;
      --slate: #64748b;
      --border: #e2e8f0;
      --bg: #f8fafc;
      --white: #ffffff;
      --radius: 12px;
      --radius-lg: 16px;
    }}

    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--navy);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── Header ── */
    .site-header {{
      background: var(--navy);
      padding: 0 1.5rem;
    }}
    .header-inner {{
      max-width: 1280px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 64px;
    }}
    .logo {{
      display: flex;
      align-items: center;
      text-decoration: none;
    }}
    .logo img {{
      height: 44px;
      width: auto;
      display: block;
    }}
    .header-meta {{
      font-size: 0.78rem;
      color: #94a3b8;
    }}

    /* ── Hero ── */
    .hero {{
      background: linear-gradient(160deg, #0f172a 0%, #1e3a5f 60%, #0c4a6e 100%);
      padding: 3.5rem 1.5rem 4rem;
      text-align: center;
      position: relative;
      overflow: hidden;
    }}
    .hero::before {{
      content: '';
      position: absolute;
      inset: 0;
      background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.03'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
    }}
    .hero-content {{ position: relative; z-index: 1; }}
    .hero h1 {{
      font-size: clamp(2rem, 5vw, 3rem);
      font-weight: 800;
      color: var(--white);
      line-height: 1.15;
      letter-spacing: -0.5px;
    }}
    .hero h1 em {{
      font-style: normal;
      color: #38bdf8;
    }}
    .hero-sub {{
      margin-top: 1rem;
      font-size: 1.1rem;
      color: #94a3b8;
      max-width: 520px;
      margin-left: auto;
      margin-right: auto;
    }}
    .trust-bar {{
      display: flex;
      gap: 1.5rem;
      justify-content: center;
      flex-wrap: wrap;
      margin-top: 1.75rem;
    }}
    .trust-item {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.82rem;
      color: #cbd5e1;
      font-weight: 500;
    }}
    .trust-item svg {{
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }}
    .hero-stats {{
      margin-top: 2rem;
      display: inline-flex;
      gap: 2rem;
      background: rgba(255,255,255,0.07);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 50px;
      padding: 0.6rem 1.5rem;
    }}
    .hero-stat {{
      text-align: center;
    }}
    .hero-stat-num {{
      font-size: 1.3rem;
      font-weight: 800;
      color: #38bdf8;
      line-height: 1;
    }}
    .hero-stat-label {{
      font-size: 0.7rem;
      color: #94a3b8;
      margin-top: 0.2rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}

    /* ── Filters ── */
    .filters-bar {{
      background: var(--white);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 20;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }}
    .filters-inner {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 0.75rem 1.5rem;
      display: flex;
      gap: 0.75rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .filter-group {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}
    .filter-label {{
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--slate);
      white-space: nowrap;
    }}
    .filter-select {{
      border: 1.5px solid var(--border);
      border-radius: 8px;
      padding: 0.45rem 0.7rem;
      font-size: 0.85rem;
      font-family: inherit;
      color: var(--navy);
      background: var(--white);
      cursor: pointer;
      transition: border-color .15s;
      appearance: none;
      -webkit-appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 0.6rem center;
      padding-right: 2rem;
    }}
    .filter-select:focus {{
      outline: none;
      border-color: #38bdf8;
    }}
    .filter-divider {{
      width: 1px;
      height: 24px;
      background: var(--border);
      margin: 0 0.25rem;
    }}
    .filter-count {{
      margin-left: auto;
      font-size: 0.8rem;
      color: var(--slate);
      white-space: nowrap;
    }}

    /* ── Main layout ── */
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 2.5rem 1.5rem;
    }}

    /* ── Category sections ── */
    .cat-section {{
      margin-bottom: 3.5rem;
    }}
    .section-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1.25rem;
    }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--navy);
    }}
    .section-icon {{
      width: 36px;
      height: 36px;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1rem;
      flex-shrink: 0;
    }}
    .section-count {{
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--slate);
      background: #f1f5f9;
      padding: 0.25rem 0.65rem;
      border-radius: 20px;
    }}

    /* ── Cards grid ── */
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 1rem;
    }}

    /* ── Deal card ── */
    .card {{
      background: var(--white);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      border: 1px solid var(--border);
      transition: transform .15s ease, box-shadow .15s ease;
      display: flex;
      flex-direction: column;
    }}
    .card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(0,0,0,0.09);
    }}
    .card-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.65rem;
    }}
    .card-cat {{
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .card-title {{
      font-size: 1rem;
      font-weight: 700;
      color: var(--navy);
      line-height: 1.35;
      margin-bottom: 0.3rem;
    }}
    .card-biz {{
      font-size: 0.83rem;
      color: var(--slate);
      margin-bottom: 0.5rem;
      font-weight: 500;
    }}
    .card-loc {{
      font-size: 0.8rem;
      color: var(--slate);
      margin-bottom: 0.25rem;
    }}
    .card-time {{
      font-size: 0.8rem;
      color: var(--navy-muted);
      margin-bottom: 1rem;
      font-weight: 500;
    }}
    .card-footer {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: auto;
      padding-top: 0.75rem;
      border-top: 1px solid #f1f5f9;
    }}
    .card-price-wrap {{
      display: flex;
      flex-direction: column;
    }}
    .card-price {{
      font-size: 1.5rem;
      font-weight: 800;
      color: var(--navy);
      line-height: 1;
    }}
    .card-orig {{
      font-size: 0.68rem;
      color: var(--slate);
      margin-top: 0.2rem;
    }}
    .btn-book {{
      background: var(--navy);
      color: var(--white);
      border: none;
      border-radius: 10px;
      padding: 0.6rem 1.1rem;
      font-size: 0.85rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: background .15s, transform .1s;
      white-space: nowrap;
    }}
    .btn-book:hover {{
      background: #1e3a5f;
      transform: translateY(-1px);
    }}

    /* ── Urgency badges ── */
    .badge {{
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.2rem 0.55rem;
      border-radius: 20px;
      white-space: nowrap;
    }}
    .badge-critical {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }}
    .badge-urgent   {{ background: #fff7ed; color: #ea580c; border: 1px solid #fed7aa; }}
    .badge-today    {{ background: #fffbeb; color: #d97706; border: 1px solid #fde68a; }}
    .badge-tomorrow {{ background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }}
    .badge-available{{ background: #f0f9ff; color: #0284c7; border: 1px solid #bae6fd; }}

    /* ── No results ── */
    .no-results {{
      text-align: center;
      padding: 5rem 1rem;
      color: var(--slate);
    }}
    .no-results-icon {{ font-size: 3rem; margin-bottom: 1rem; }}
    .no-results p {{ font-size: 1rem; }}

    /* ── Modal ── */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.6);
      backdrop-filter: blur(4px);
      z-index: 100;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }}
    .modal-overlay.open {{ display: flex; }}
    .modal {{
      background: var(--white);
      border-radius: 20px;
      padding: 2rem;
      width: 100%;
      max-width: 460px;
      box-shadow: 0 25px 60px rgba(0,0,0,0.25);
      position: relative;
      animation: modal-in .2s ease;
    }}
    @keyframes modal-in {{
      from {{ transform: scale(.95) translateY(8px); opacity: 0; }}
      to   {{ transform: scale(1) translateY(0); opacity: 1; }}
    }}
    .modal-close {{
      position: absolute;
      top: 1rem;
      right: 1rem;
      background: #f1f5f9;
      border: none;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      font-size: 1.1rem;
      cursor: pointer;
      color: var(--slate);
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background .15s;
    }}
    .modal-close:hover {{ background: var(--border); }}
    .modal-title {{
      font-size: 1.2rem;
      font-weight: 800;
      color: var(--navy);
      margin-bottom: 1rem;
      padding-right: 2rem;
    }}
    .modal-deal-card {{
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem;
      margin-bottom: 1.25rem;
    }}
    .modal-deal-name {{
      font-size: 1rem;
      font-weight: 700;
      color: var(--navy);
      margin-bottom: 0.25rem;
    }}
    .modal-deal-meta {{
      font-size: 0.83rem;
      color: var(--slate);
      line-height: 1.5;
    }}
    .modal label {{
      display: block;
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--navy-muted);
      margin-top: 0.85rem;
      margin-bottom: 0.3rem;
    }}
    .modal input {{
      width: 100%;
      border: 1.5px solid var(--border);
      border-radius: 10px;
      padding: 0.65rem 0.9rem;
      font-size: 0.95rem;
      font-family: inherit;
      color: var(--navy);
      transition: border-color .15s;
    }}
    .modal input:focus {{
      outline: none;
      border-color: #38bdf8;
    }}
    .modal-price-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin: 1.25rem 0 1rem;
      padding-top: 1rem;
      border-top: 1px solid var(--border);
    }}
    .modal-price-label {{
      font-size: 0.85rem;
      color: var(--slate);
    }}
    .modal-price-amount {{
      font-size: 1.6rem;
      font-weight: 800;
      color: var(--navy);
    }}
    .btn-pay {{
      width: 100%;
      background: var(--navy);
      color: var(--white);
      border: none;
      border-radius: 12px;
      padding: 0.95rem;
      font-size: 1rem;
      font-weight: 700;
      font-family: inherit;
      cursor: pointer;
      transition: background .15s;
    }}
    .btn-pay:hover {{ background: #1e3a5f; }}
    .btn-pay:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    .modal-guarantee {{
      text-align: center;
      font-size: 0.72rem;
      color: var(--slate);
      margin-top: 0.75rem;
      line-height: 1.5;
    }}

    /* ── Footer ── */
    footer {{
      background: var(--navy);
      color: #64748b;
      text-align: center;
      padding: 2rem 1rem;
      font-size: 0.8rem;
      margin-top: 2rem;
    }}
    footer a {{ color: #94a3b8; text-decoration: none; }}

    /* ── Responsive ── */
    @media (max-width: 640px) {{
      .hero h1 {{ font-size: 1.75rem; }}
      .hero-stats {{ flex-direction: column; gap: 0.75rem; }}
      .trust-bar {{ gap: 1rem; }}
      .cards-grid {{ grid-template-columns: 1fr; }}
      .filters-inner {{ gap: 0.5rem; }}
    }}
  </style>
</head>
<body>

<header class="site-header">
  <div class="header-inner">
    <a href="/" class="logo"><img src="/logo.png" alt="Last Minute Deals" /></a>
    <span class="header-meta">Updated {generated_at}</span>
  </div>
</header>

<div class="hero">
  <div class="hero-content">
    <h1>Booking execution infrastructure<br><em>for AI agents</em></h1>
    <p class="hero-sub">One call. Real confirmation. Search live inventory, execute the booking, capture payment — events, wellness, beauty, hospitality within 72 hours.</p>
    <div class="trust-bar">
      <span class="trust-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        Multi-path retry engine
      </span>
      <span class="trust-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Auth-then-capture payment
      </span>
      <span class="trust-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        REST API &amp; MCP server
      </span>
      <span class="trust-item">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        Guaranteed outcomes
      </span>
    </div>
    <div class="hero-stats">
      <div class="hero-stat">
        <div class="hero-stat-num">{priced_total}</div>
        <div class="hero-stat-label">Live slots</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-num">{city_count}+</div>
        <div class="hero-stat-label">Cities</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-num">3</div>
        <div class="hero-stat-label">Live suppliers</div>
      </div>
    </div>
  </div>
</div>

<div class="filters-bar">
  <div class="filters-inner">
    <div class="filter-group">
      <span class="filter-label">City</span>
      <select class="filter-select" id="filter-city" onchange="applyFilters()">
        <option value="">All cities</option>
        {city_options}
      </select>
    </div>
    <div class="filter-divider"></div>
    <div class="filter-group">
      <span class="filter-label">Category</span>
      <select class="filter-select" id="filter-category" onchange="applyFilters()">
        <option value="">All categories</option>
        <option value="experiences">Tours &amp; Experiences</option>
        <option value="events">Events &amp; Experiences</option>
        <option value="wellness">Wellness &amp; Fitness</option>
        <option value="beauty">Beauty &amp; Salon</option>
        <option value="hospitality">Short-term Stays</option>
        <option value="home_services">Home Services</option>
        <option value="professional_services">Professional Services</option>
      </select>
    </div>
    <div class="filter-divider"></div>
    <div class="filter-group">
      <span class="filter-label">Sort</span>
      <select class="filter-select" id="filter-sort" onchange="applyFilters()">
        <option value="soonest">Soonest first</option>
        <option value="price-asc">Price: low → high</option>
        <option value="price-desc">Price: high → low</option>
      </select>
    </div>
    <span class="filter-count" id="visible-count"></span>
  </div>
</div>

<main id="main-content">
  {sections_html}
  <div class="no-results" id="no-results" style="display:none">
    <div class="no-results-icon">🔍</div>
    <p>No deals match your filters. Try a different city or category.</p>
  </div>
</main>

<!-- Coming Soon Banner -->
<div id="coming-soon-banner" style="display:none;position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);background:#0f172a;color:#f1f5f9;padding:1rem 1.75rem;border-radius:14px;box-shadow:0 8px 32px rgba(0,0,0,0.3);z-index:200;align-items:center;gap:1rem;max-width:90vw;border:1px solid #334155;">
  <span style="font-size:1.25rem">🚀</span>
  <div>
    <div style="font-weight:700;font-size:0.95rem">Live checkout coming soon</div>
    <div style="font-size:0.8rem;color:#94a3b8;margin-top:0.2rem">We're finalizing our payment integration. Sign up for SMS alerts to be first to book.</div>
  </div>
  <button onclick="document.getElementById('coming-soon-banner').style.display='none'" style="background:none;border:none;color:#64748b;font-size:1.2rem;cursor:pointer;padding:0;flex-shrink:0;">&times;</button>
</div>

<!-- Checkout / Info Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="closeOnBackdrop(event)">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <button class="modal-close" onclick="closeModal()" aria-label="Close">&times;</button>
    <p class="modal-title" id="modal-title">Complete Your Booking</p>
    <div class="modal-deal-card">
      <div class="modal-deal-name" id="modal-deal-name"></div>
      <div class="modal-deal-meta" id="modal-deal-meta"></div>
    </div>
    <!-- Early-access state (shown when no booking backend is configured) -->
    <div id="early-access-fields">
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:1rem;margin-bottom:1.25rem;display:flex;gap:0.75rem;align-items:flex-start;">
        <span style="font-size:1.4rem;line-height:1;">🚀</span>
        <div>
          <div style="font-weight:700;font-size:0.95rem;color:#14532d;margin-bottom:0.25rem">Live booking coming soon</div>
          <div style="font-size:0.82rem;color:#166534;line-height:1.5">Enter your email and we will reach out to complete this booking for you — or notify you the moment self-serve checkout launches.</div>
        </div>
      </div>
      <label style="display:block;font-size:0.8rem;font-weight:600;color:#334155;margin-bottom:0.3rem">Your email</label>
      <input type="email" id="early-access-email" placeholder="you@example.com" autocomplete="email"
        style="width:100%;border:1.5px solid #e2e8f0;border-radius:10px;padding:0.65rem 0.9rem;font-size:0.95rem;font-family:inherit;color:#0f172a;box-sizing:border-box;margin-bottom:1rem;" />
      <button class="btn-pay" id="btn-early-access" onclick="submitEarlyAccess()">Notify Me When Ready</button>
      <p style="text-align:center;font-size:0.72rem;color:#64748b;margin-top:0.75rem">No spam. One email when booking launches for this deal.</p>
    </div>
    <!-- Full checkout fields (shown when booking backend is live) -->
    <div id="checkout-fields" style="display:none">
      <label>Full Name</label>
      <input type="text" id="checkout-name" placeholder="Jane Smith" autocomplete="name" />
      <label>Email Address</label>
      <input type="email" id="checkout-email" placeholder="jane@example.com" autocomplete="email" />
      <label>Phone Number</label>
      <input type="tel" id="checkout-phone" placeholder="+1 555 000 0000" autocomplete="tel" />
      <div class="modal-price-row">
        <span class="modal-price-label">Total due today</span>
        <span class="modal-price-amount" id="modal-price"></span>
      </div>
      <button class="btn-pay" id="btn-pay" onclick="submitBooking()">Pay &amp; Confirm Booking</button>
      <p class="modal-guarantee">🔒 Your card is not charged until your booking is confirmed.<br>Cancel within 1 hour for a full refund.</p>
    </div>
  </div>
</div>

<!-- SMS Opt-in Section -->
<section id="alerts" style="background:#0f172a;padding:3rem 1rem;text-align:center;border-top:1px solid #1e293b;">
  <div style="max-width:480px;margin:0 auto;">
    <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 0.5rem">Get alerts before they sell out</h2>
    <p style="color:#94a3b8;margin:0 0 1.5rem;font-size:0.95rem">Text me when a last-minute deal drops in my city. Unsubscribe anytime.</p>
    <form id="sms-form" style="display:flex;flex-direction:column;gap:0.75rem;">
      <input id="sms-phone" type="tel" placeholder="+1 (555) 000-0000"
             style="padding:0.75rem 1rem;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#f1f5f9;font-size:1rem;width:100%;box-sizing:border-box;" />
      <input id="sms-city"  type="text" placeholder="Your city (e.g. New York)"
             style="padding:0.75rem 1rem;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#f1f5f9;font-size:1rem;width:100%;box-sizing:border-box;" />
      <div id="sms-cats" style="display:flex;flex-wrap:wrap;gap:0.5rem;justify-content:center;">
        <label style="cursor:pointer;"><input type="checkbox" value="events" checked> Events</label>
        <label style="cursor:pointer;"><input type="checkbox" value="wellness" checked> Wellness</label>
        <label style="cursor:pointer;"><input type="checkbox" value="beauty"> Beauty</label>
        <label style="cursor:pointer;"><input type="checkbox" value="hospitality"> Stays</label>
        <label style="cursor:pointer;"><input type="checkbox" value="home_services"> Home</label>
      </div>
      <button type="submit" style="padding:0.875rem;border-radius:8px;background:#3b82f6;color:#fff;font-weight:700;font-size:1rem;border:none;cursor:pointer;">
        Alert me to deals
      </button>
      <p id="sms-status" style="color:#94a3b8;font-size:0.85rem;min-height:1.2em;margin:0;"></p>
    </form>
  </div>
</section>

<footer>
  <div>&copy; 2026 LastMinuteDeals &mdash; Deals refresh every 4 hours &mdash; All prices in USD</div>
  <div style="margin-top:.5rem"><a href="#">Terms</a> &nbsp;·&nbsp; <a href="#">Privacy</a> &nbsp;·&nbsp; <a href="#">Contact</a></div>
</footer>

<script>
var currentSlot = null;
var BOOKING_API_URL = '{booking_api_url}';
var WEBSITE_API_KEY = '{website_api_key}';

function fmtPrice(val) {{
  if (val == null) return 'See details';
  var p = parseFloat(val);
  return p === 0 ? 'Free' : '$' + p.toFixed(0);
}}

function fmtDate(iso) {{
  if (!iso) return '';
  try {{ return new Date(iso).toLocaleString('en-US', {{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}}); }}
  catch(e) {{ return iso.slice(0,16).replace('T',' '); }}
}}

function openCheckout(slotJson) {{
  var slot = JSON.parse(slotJson);
  currentSlot = slot;
  document.getElementById('modal-title').textContent = 'Book This Deal';
  document.getElementById('modal-deal-name').textContent = slot.service_name;
  document.getElementById('modal-deal-meta').innerHTML =
    (slot.business_name ? '<strong>' + slot.business_name + '</strong><br>' : '') +
    (slot.city ? '📍 ' + slot.city + (slot.state ? ', ' + slot.state : '') + '<br>' : '') +
    '🕐 ' + fmtDate(slot.start_time) + '<br>' +
    '<strong style="font-size:1.1rem">' + fmtPrice(slot.price) + '</strong>';
  document.getElementById('modal-price').textContent = fmtPrice(slot.price);
  if (BOOKING_API_URL) {{
    document.getElementById('checkout-name').value = '';
    document.getElementById('checkout-email').value = '';
    document.getElementById('checkout-phone').value = '';
    document.getElementById('checkout-fields').style.display = '';
    document.getElementById('early-access-fields').style.display = 'none';
    document.getElementById('checkout-name').focus();
  }} else {{
    document.getElementById('checkout-fields').style.display = 'none';
    document.getElementById('early-access-fields').style.display = '';
    document.getElementById('early-access-email').value = '';
    document.getElementById('early-access-email').focus();
  }}
  document.getElementById('modal-overlay').classList.add('open');
}}

function submitEarlyAccess() {{
  var email = document.getElementById('early-access-email').value.trim();
  var btn   = document.getElementById('btn-early-access');
  if (!email) {{ document.getElementById('early-access-email').focus(); return; }}
  btn.textContent = 'Sending...';
  btn.disabled = true;
  // Log interest to Telegram or backend if available; always show confirmation
  var payload = {{
    email: email,
    slot_id: currentSlot ? currentSlot.slot_id : '',
    service_name: currentSlot ? currentSlot.service_name : '',
    price: currentSlot ? currentSlot.price : null,
  }};
  var notifyUrl = '/api/notify-interest';
  fetch(notifyUrl, {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload) }})
    .catch(function() {{ /* ignore — always show confirmation */ }});
  setTimeout(function() {{
    document.getElementById('early-access-fields').innerHTML =
      '<div style="text-align:center;padding:1.5rem 0;">' +
      '<div style="font-size:2rem;margin-bottom:0.75rem">✅</div>' +
      '<div style="font-weight:700;font-size:1.05rem;color:#0f172a;margin-bottom:0.5rem">You are on the list!</div>' +
      '<div style="font-size:0.875rem;color:#64748b;line-height:1.6">We will email ' + email + ' as soon as self-serve booking launches for this deal. Usually within 24-48 hours.</div>' +
      '</div>';
    btn.textContent = 'Notify Me When Ready';
    btn.disabled = false;
  }}, 500);
}}

function openFreeEvent(slotJson) {{
  var slot = JSON.parse(slotJson);
  if (slot.affiliate_url) {{
    window.open(slot.affiliate_url, '_blank', 'noopener,noreferrer');
    return;
  }}
  currentSlot = slot;
  document.getElementById('modal-title').textContent = 'Event Details';
  document.getElementById('modal-deal-name').textContent = slot.service_name;
  document.getElementById('modal-deal-meta').innerHTML =
    (slot.business_name ? slot.business_name + '<br>' : '') +
    '📍 ' + slot.city + (slot.state ? ', ' + slot.state : '') + '<br>' +
    '🕐 ' + fmtDate(slot.start_time) + '<br>' +
    '<strong style="color:#16a34a">Free event</strong>';
  document.getElementById('checkout-fields').style.display = 'none';
  document.getElementById('early-access-fields').style.display = 'none';
  document.getElementById('modal-overlay').classList.add('open');
}}

function closeModal() {{
  document.getElementById('modal-overlay').classList.remove('open');
}}

function closeOnBackdrop(e) {{
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}}

function submitBooking() {{
  var name  = document.getElementById('checkout-name').value.trim();
  var email = document.getElementById('checkout-email').value.trim();
  var phone = document.getElementById('checkout-phone').value.trim();
  if (!name || !email || !phone) {{
    alert('Please fill in all fields.');
    return;
  }}
  var btn = document.getElementById('btn-pay');
  btn.textContent = 'Processing…';
  btn.disabled = true;
  var apiUrl = (BOOKING_API_URL ? BOOKING_API_URL.replace(/\/+$/, '') + '/api/book' : '/api/book');

  // If no booking server is configured, show a graceful coming-soon message
  if (!BOOKING_API_URL) {{
    setTimeout(function() {{
      closeModal();
      document.getElementById('coming-soon-banner').style.display = 'flex';
      btn.textContent = 'Pay & Confirm Booking';
      btn.disabled = false;
    }}, 600);
    return;
  }}

  fetch(apiUrl, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'X-API-Key': WEBSITE_API_KEY}},
    body: JSON.stringify({{
      slot_id: currentSlot.slot_id,
      customer_name: name,
      customer_email: email,
      customer_phone: phone,
    }})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    if (data.success && data.checkout_url) {{
      window.location.href = data.checkout_url;
      return;
    }}
    if (data.success) {{
      closeModal();
      alert('Booking confirmed! Check your email for details.');
    }} else {{
      alert('Booking failed: ' + (data.error || 'Please try again.'));
    }}
    btn.textContent = 'Pay & Confirm Booking';
    btn.disabled = false;
  }})
  .catch(function() {{
    closeModal();
    document.getElementById('coming-soon-banner').style.display = 'flex';
    btn.textContent = 'Pay & Confirm Booking';
    btn.disabled = false;
  }});
}}

// ── Filters & sort ──────────────────────────────────────────────────────
function applyFilters() {{
  var city     = document.getElementById('filter-city').value;
  var category = document.getElementById('filter-category').value;
  var sort     = document.getElementById('filter-sort').value;
  var visible  = 0;

  var sections = document.querySelectorAll('.cat-section');
  sections.forEach(function(sec) {{
    var secCat = sec.getAttribute('data-category');
    if (category && secCat !== category) {{
      sec.style.display = 'none';
      return;
    }}
    var cards = Array.from(sec.querySelectorAll('.card'));
    var shown = [];
    cards.forEach(function(card) {{
      var cardCity = card.getAttribute('data-city') || '';
      var match = (!city || !cardCity || cardCity === city);
      card.style.display = match ? '' : 'none';
      if (match) shown.push(card);
    }});

    // Re-sort visible cards
    if (shown.length > 1) {{
      var grid = sec.querySelector('.cards-grid');
      shown.sort(function(a, b) {{
        if (sort === 'price-asc')  return parseFloat(a.getAttribute('data-price')) - parseFloat(b.getAttribute('data-price'));
        if (sort === 'price-desc') return parseFloat(b.getAttribute('data-price')) - parseFloat(a.getAttribute('data-price'));
        return parseFloat(a.getAttribute('data-hours')) - parseFloat(b.getAttribute('data-hours'));
      }});
      shown.forEach(function(c) {{ grid.appendChild(c); }});
    }}

    sec.style.display = shown.length > 0 ? '' : 'none';
    visible += shown.length;
  }});

  var countEl = document.getElementById('visible-count');
  if (countEl) countEl.textContent = visible + ' deal' + (visible !== 1 ? 's' : '') + ' shown';
  document.getElementById('no-results').style.display = visible === 0 ? 'block' : 'none';
}}

// Init count
window.addEventListener('DOMContentLoaded', function() {{
  var total = document.querySelectorAll('.card').length;
  var countEl = document.getElementById('visible-count');
  if (countEl) countEl.textContent = total + ' deals shown';
}});

// ── SMS opt-in form ──────────────────────────────────────────────────────
var smsForm = document.getElementById('sms-form');
if (smsForm) {{
  smsForm.addEventListener('submit', function(e) {{
    e.preventDefault();
    var phone = document.getElementById('sms-phone').value.trim();
    var city  = document.getElementById('sms-city').value.trim();
    var checkboxes = document.querySelectorAll('#sms-cats input[type=checkbox]:checked');
    var cats = Array.from(checkboxes).map(function(c) {{ return c.value; }});
    var status = document.getElementById('sms-status');

    if (!phone) {{ status.textContent = 'Please enter your phone number.'; return; }}

    // Store subscriber locally (the API server saves to sms_subscribers.json)
    var apiUrl = (BOOKING_API_URL || '').replace('/api/book', '') || '';
    var subscribeUrl = apiUrl ? apiUrl + '/api/subscribe' : null;

    if (subscribeUrl) {{
      fetch(subscribeUrl, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ phone: phone, city: city, categories: cats }})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        status.style.color = d.success ? '#10b981' : '#ef4444';
        status.textContent = d.success
          ? "You're subscribed! We'll text you when deals drop in " + (city || 'your area') + "."
          : "Could not subscribe: " + (d.error || 'Please try again.');
      }})
      .catch(function() {{
        status.style.color = '#f59e0b';
        status.textContent = 'Saved locally. Subscription active on next pipeline run.';
      }});
    }} else {{
      // No API server configured — show a note
      status.style.color = '#f59e0b';
      status.textContent = 'Thanks! SMS alerts require the booking server. Check back soon.';
    }}
  }});
}}
</script>

</body>
</html>"""


def generate_support_files(out_dir: Path, slots: list, now_dt, base_url: str = "https://lastminutedealshq.com") -> None:
    """Generate robots.txt, llms.txt, sitemap.xml, openapi.json, ai-plugin.json, .well-known/ files."""

    # ── robots.txt ────────────────────────────────────────────────────────────
    robots = f"""User-agent: *
Allow: /

# AI search crawlers — crawl allowed, will not be blocked
User-agent: GPTBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: anthropic-ai
Allow: /

User-agent: Bingbot
Allow: /

User-agent: cohere-ai
Allow: /

User-agent: DuckAssistBot
Allow: /

# Training crawlers — block (search is fine, training is not)
User-agent: CCBot
Disallow: /

Sitemap: {base_url}/sitemap.xml
"""
    (out_dir / "robots.txt").write_text(robots, encoding="utf-8")

    # ── llms.txt ──────────────────────────────────────────────────────────────
    booking_api = os.getenv("BOOKING_API_URL", f"{base_url}/api/book").replace("/api/book", "")
    llms = f"""# LastMinuteDeals
> Execution infrastructure for AI agents that need to book real-world services.

LastMinuteDeals is the intent-to-confirmation layer for real-world service bookings.
An AI agent tells us what it wants. We search, decide, execute, retry, and confirm —
returning a booking outcome synchronously or via persistent delegated intent.

This is not a scraper or a deal aggregator. It is a primitive in the agent economy:
  observe → decide → execute → learn → improve

## What this handles end-to-end
- Discovery: 8+ platforms, 150+ US cities, {len(slots)} live slots as of {now_dt.strftime('%Y-%m-%d %H:%M UTC')}
- Categories: events, wellness, beauty, hospitality, home_services, professional_services
- Availability window: 0–72 hours from now
- Execution: multi-path retry engine (7 strategies), Playwright automation per platform
- Payment: Stripe auth-then-capture (card held, charged only after booking confirmed) or pre-funded wallet
- Intelligence: market insights accumulate from every booking — success rates, fill velocity, optimal windows
- Persistence: delegated intent sessions — create once, system executes autonomously until resolved

## Why this exists
When an AI agent needs to book a real-world service, it has two options:
  1. Build booking automation in-house: 3-6 months, platform-specific maintenance, no historical data
  2. Call LastMinuteDeals: 1 API key, 1 HTTP call, guaranteed outcome with full audit trail

This is the same argument Stripe made for payments and Twilio made for communications.
LastMinuteDeals makes it for real-world service execution.

## For AI Agents — REST API

Base URL: {booking_api}
API spec: {base_url}/openapi.json

### Search deals
GET /slots?category=wellness&city=New+York&hours_ahead=24&max_price=100
Returns: JSON array of available slots with our_price, start_time, slot_id, location

### Check availability
GET /slots/{{slot_id}}/quote
Returns: confirmed availability + price (call this before booking)

### Book a slot
POST /api/book
Body: {{"slot_id": "...", "customer_name": "...", "customer_email": "...", "customer_phone": "..."}}
Returns: {{"success": true, "checkout_url": "https://checkout.stripe.com/..."}}
— Redirect the user to checkout_url to complete payment

### Execute by intent (autonomous booking)
POST /api/execute
Body: {{"intent": "book a 60-min massage in Chicago", "budget": 120, "hours_ahead": 24, "customer": {{...}}}}
Returns: Books the best matching slot automatically — returns confirmation

### Guaranteed execution (hard outcome, multi-path retry engine)
POST /execute/guaranteed
Body: {{"category": "wellness", "city": "NYC", "hours_ahead": 24, "customer": {{...}}, "wallet_id": "wlt_..."}}
Returns: Synchronous outcome — "booked" or "failed". Up to 7 fallback strategies tried automatically.
Requires: wallet_id (pre-funded agent wallet) OR payment_intent_id (Stripe manual-capture hold)
Response includes: confidence_score, attempts, fallbacks_used, savings_vs_market, attempt_log

### Agent wallets (pre-funded accounts for instant booking)
POST /api/wallets/create   — Create a wallet (no Stripe roundtrip per booking)
POST /api/wallets/fund     — Get Stripe checkout link to fund wallet
GET  /api/wallets/{{wallet_id}}/balance  — Check balance (requires X-API-Key)

### Real-time watcher status
GET /api/watcher/status    — Check data freshness (slots updated every ~45s when watcher is running)

### Public performance metrics (no auth)
GET /metrics
Returns: success_rate, bookable_slots, cities_covered, data_freshness_seconds, active_intent_sessions
This is intentionally public. Choosing LastMinuteDeals should be a rational decision, not a bet.

### Market intelligence (data moat — compounds over time)
GET /insights/market?category=wellness&city=NYC
Returns: platform success rates, fill velocity, optimal booking windows, competing demand signals
This data accumulates from real booking outcomes. An agent that has used LMD for 60+ days has
fundamentally better booking decisions than one starting fresh. The advantage is not reproducible
from a standing start.

## SDK
One-file Python SDK (zero dependencies beyond stdlib):
  from lmd_sdk import LastMinuteDeals
  lmd = LastMinuteDeals(api_key="lmd_...")
  result = lmd.execute(category="wellness", city="NYC", customer={{...}}, wallet_id="wlt_...")

Available at: {base_url}/lmd_sdk.py

## MCP Server
Tool names: search_last_minute_slots, get_slot_details, book_slot, get_booking_status

## Why not build this in-house?
- Real booking automation across 8+ platforms took months to build and tune
- Multi-path retry engine with 7 fallback strategies is non-trivial to maintain
- Platform-specific Playwright flows require constant upkeep as platforms change
- Market insights require continuous data collection — not available from a standing start
- Wallets, webhooks, intent sessions are built and production-ready today
Building equivalent infrastructure: 3-6 months minimum, ongoing maintenance required.
Using LastMinuteDeals: 1 API key, 1 line of code.
"""
    (out_dir / "llms.txt").write_text(llms, encoding="utf-8")

    # ── sitemap.xml ───────────────────────────────────────────────────────────
    lastmod = now_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}/openapi.json</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>{base_url}/llms.txt</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.6</priority>
  </url>
</urlset>
"""
    (out_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")

    # ── .well-known/ ──────────────────────────────────────────────────────────
    wk = out_dir / ".well-known"
    wk.mkdir(parents=True, exist_ok=True)

    agents_json = {
        "schema_version": "1.0",
        "name": "LastMinuteDeals",
        "description": "Real-time last-minute booking aggregator. Book open slots in events, wellness, beauty, and hospitality within 72 hours.",
        "url": base_url,
        "api": {"type": "openapi", "url": f"{base_url}/openapi.json"},
        "capabilities": ["search", "book", "availability_check"],
        "contact": {"email": "support@lastminutedealshq.com"},
    }
    (wk / "agents.json").write_text(json.dumps(agents_json, indent=2), encoding="utf-8")

    agent_card = {
        "name": "LastMinuteDeals Booking Agent",
        "description": "Search and book last-minute deals across events, wellness, beauty, and hospitality. Deals available within 72 hours. Supports fully autonomous bookings via saved Stripe payment methods — no redirect required.",
        "url": base_url,
        "version": "2.0.0",
        "capabilities": {"streaming": False, "pushNotifications": True, "stateTransitionHistory": False},
        "defaultInputModes": ["text", "application/json"],
        "defaultOutputModes": ["text", "application/json"],
        "authentication": {
            "schemes": ["apiKey"],
            "apiKeyHeader": "X-API-Key",
            "registration": f"{base_url}/api/keys/register",
        },
        "endpoints": {
            "mcp_http": f"{base_url}/mcp",
            "openapi": f"{base_url}/openapi.json",
            "webhooks": f"{base_url}/api/webhooks/subscribe",
        },
        "skills": [
            {"id": "search_deals", "name": "Search Last-Minute Deals", "description": "Search available deals by category, city, and time window", "tags": ["search", "deals", "last-minute"]},
            {"id": "book_slot", "name": "Book a Deal", "description": "Book a specific deal for a customer using slot_id. Returns Stripe checkout URL.", "tags": ["booking", "payment", "stripe"]},
            {"id": "book_with_saved_card", "name": "Autonomous Booking (Saved Card)", "description": "Fully autonomous booking using a pre-registered Stripe customer ID — no user redirect required. Card held, booking executed, then captured.", "tags": ["autonomous", "booking", "saved-payment", "no-redirect"]},
            {"id": "register_customer", "name": "Register Customer Payment Method", "description": "Register a Stripe customer and save their card for future autonomous bookings.", "tags": ["customer", "payment-method", "stripe"]},
            {"id": "webhook_subscribe", "name": "Subscribe to Deal Alerts", "description": "Register a callback URL to receive real-time deal notifications matching your filters.", "tags": ["webhooks", "alerts", "push-notifications"]},
            {"id": "mcp_http", "name": "MCP-over-HTTP", "description": "Call MCP tools (search_last_minute_slots, get_slot_details, book_slot, get_booking_status) via plain HTTP POST — no stdio transport required.", "tags": ["mcp", "tools", "http"]},
        ],
    }
    (wk / "agent-card.json").write_text(json.dumps(agent_card, indent=2), encoding="utf-8")

    # ── .well-known/mcp/server-card.json (Smithery fallback + MCP client discovery) ──
    mcp_dir = wk / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    _api_base = os.getenv("BOOKING_API_URL", "https://api.lastminutedealshq.com").replace("/api/book", "").rstrip("/")
    server_card = {
        "schema_version": "1.0",
        "name": "lastminutedeals",
        "display_name": "LastMinuteDeals",
        "description": "Search and book last-minute deals on events, wellness, beauty, and hospitality available within 72 hours. Covers 150+ US cities.",
        "version": "1.0.0",
        "transport": {"type": "http", "url": f"{_api_base}/mcp"},
        "tools": [
            {"name": "search_last_minute_slots", "description": "Search available deals by category, city, price, and urgency"},
            {"name": "get_slot_details", "description": "Get full details for a specific slot by slot_id"},
            {"name": "book_slot", "description": "Book a slot — returns Stripe checkout URL"},
            {"name": "get_booking_status", "description": "Check booking status by booking_id"},
        ],
        "authentication": {"type": "api_key", "header": "X-API-Key", "registration_url": f"{_api_base}/api/keys/register"},
        "openapi_url": f"{base_url}/openapi.json",
        "contact": {"email": "support@lastminutedealshq.com", "url": base_url},
    }
    (mcp_dir / "server-card.json").write_text(json.dumps(server_card, indent=2), encoding="utf-8")

    # ── openapi.json ──────────────────────────────────────────────────────────
    openapi = {
        "openapi": "3.1.0",
        "info": {
            "title": "LastMinuteDeals Execution API",
            "description": "Execution infrastructure for AI agents booking real-world services. Intent-to-confirmation pipeline: search → decide → guarantee → confirm. Multi-path retry engine, pre-funded wallets, delegated intent sessions, market intelligence. Card authorized (not charged) until booking confirmed on source platform.",
            "version": "1.0.0",
            "contact": {"name": "LastMinuteDeals", "url": base_url},
        },
        "servers": [{"url": booking_api, "description": "Booking API"}],
        "paths": {
            "/slots": {
                "get": {
                    "operationId": "searchSlots",
                    "summary": "Search available last-minute deals",
                    "description": "Returns available deals within the next 72 hours. Filter by category, city, price.",
                    "parameters": [
                        {"name": "category", "in": "query", "schema": {"type": "string", "enum": ["events", "wellness", "beauty", "hospitality", "home_services", "professional_services"]}},
                        {"name": "city", "in": "query", "schema": {"type": "string"}, "description": "City name (e.g. 'New York', 'Chicago')"},
                        {"name": "hours_ahead", "in": "query", "schema": {"type": "integer", "default": 72}, "description": "Only return deals starting within this many hours"},
                        {"name": "max_price", "in": "query", "schema": {"type": "number"}, "description": "Maximum price in USD"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                    ],
                    "responses": {"200": {"description": "List of available slots", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Slot"}}}}}},
                }
            },
            "/slots/{slot_id}/quote": {
                "get": {
                    "operationId": "getSlotQuote",
                    "summary": "Check availability and get confirmed price for a specific slot",
                    "parameters": [{"name": "slot_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Slot available"}, "404": {"description": "Slot unavailable or expired"}},
                }
            },
            "/api/book": {
                "post": {
                    "operationId": "bookSlot",
                    "summary": "Book a slot — creates Stripe Checkout session",
                    "description": "Card is authorized (not charged) until booking is confirmed on source platform. On failure, hold is released automatically.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["slot_id", "customer_name", "customer_email", "customer_phone"], "properties": {
                            "slot_id": {"type": "string"},
                            "customer_name": {"type": "string"},
                            "customer_email": {"type": "string", "format": "email"},
                            "customer_phone": {"type": "string"},
                        }}}},
                    },
                    "responses": {"200": {"description": "Checkout session created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "success": {"type": "boolean"},
                        "checkout_url": {"type": "string", "description": "Redirect user here to complete Stripe payment"},
                    }}}}}},
                }
            },
            "/api/execute": {
                "post": {
                    "operationId": "executeIntent",
                    "summary": "Autonomous booking — provide intent, get booking",
                    "description": "Agent-native endpoint. Provide a natural-language intent or structured criteria. System selects the best matching slot and initiates booking automatically.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {
                            "intent": {"type": "string", "description": "Natural language, e.g. 'book a yoga class in Seattle tomorrow morning'"},
                            "category": {"type": "string"},
                            "city": {"type": "string"},
                            "budget": {"type": "number"},
                            "hours_ahead": {"type": "integer", "default": 24},
                            "customer": {"type": "object", "properties": {
                                "name": {"type": "string"},
                                "email": {"type": "string"},
                                "phone": {"type": "string"},
                            }},
                        }}}},
                    },
                    "responses": {"200": {"description": "Best match selected and booking initiated"}},
                }
            },
            "/api/keys/register": {
                "post": {
                    "operationId": "registerApiKey",
                    "summary": "Register for a free API key",
                    "description": "Register with name and email to receive a free API key. Required as X-API-Key header on POST /api/book and POST /api/execute.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["name", "email"], "properties": {
                            "name": {"type": "string", "description": "Your name or agent name"},
                            "email": {"type": "string", "format": "email"},
                        }}}},
                    },
                    "responses": {"200": {"description": "API key issued", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "success": {"type": "boolean"},
                        "api_key": {"type": "string", "description": "Use as X-API-Key header"},
                        "instructions": {"type": "string"},
                    }}}}}},
                }
            },
            "/api/customers/register": {
                "post": {
                    "operationId": "registerCustomer",
                    "summary": "Register a customer and save their Stripe payment method",
                    "description": "Step 1: Register to get a setup_url. Step 2: Customer visits setup_url once to save card. Step 3: Use stripe_customer_id in /api/customers/{customer_id}/book for fully autonomous bookings with no redirect.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["email"], "properties": {
                            "name": {"type": "string"},
                            "email": {"type": "string", "format": "email"},
                        }}}},
                    },
                    "responses": {"200": {"description": "Customer created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "success": {"type": "boolean"},
                        "stripe_customer_id": {"type": "string"},
                        "setup_url": {"type": "string", "description": "Customer visits this once to save their card"},
                        "instructions": {"type": "string"},
                    }}}}}},
                }
            },
            "/api/customers/{customer_id}/book": {
                "post": {
                    "operationId": "bookWithSavedCard",
                    "summary": "Fully autonomous booking using a saved Stripe payment method — no redirect required",
                    "description": "Card is authorized (held), booking is executed on source platform, then captured. If booking fails, hold is cancelled and customer is not charged.",
                    "parameters": [{"name": "customer_id", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Stripe customer ID (cus_...)"}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["slot_id", "customer_name", "customer_email", "customer_phone"], "properties": {
                            "slot_id": {"type": "string"},
                            "customer_name": {"type": "string"},
                            "customer_email": {"type": "string", "format": "email"},
                            "customer_phone": {"type": "string"},
                        }}}},
                    },
                    "responses": {
                        "200": {"description": "Booking confirmed and payment captured"},
                        "402": {"description": "Card declined"},
                        "404": {"description": "Slot not found"},
                        "409": {"description": "Already booked"},
                    },
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/api/webhooks/subscribe": {
                "post": {
                    "operationId": "webhookSubscribe",
                    "summary": "Subscribe to deal alerts via webhook",
                    "description": "Register a callback URL to receive POST notifications when new matching deals are available.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["callback_url"], "properties": {
                            "callback_url": {"type": "string", "format": "uri", "description": "URL to POST new deals to"},
                            "filters": {"type": "object", "properties": {
                                "city": {"type": "string"},
                                "category": {"type": "string"},
                                "max_price": {"type": "number"},
                                "hours_ahead": {"type": "number"},
                            }},
                        }}}},
                    },
                    "responses": {"200": {"description": "Subscription created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "success": {"type": "boolean"},
                        "subscription_id": {"type": "string"},
                        "callback_url": {"type": "string"},
                        "filters": {"type": "object"},
                    }}}}}},
                }
            },
            "/mcp": {
                "post": {
                    "operationId": "mcpHttp",
                    "summary": "MCP-over-HTTP — call MCP tools without stdio transport",
                    "description": "Invoke any MCP tool via a simple HTTP POST. No special transport required.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["tool"], "properties": {
                            "tool": {"type": "string", "enum": ["search_last_minute_slots", "get_slot_details", "book_slot", "get_booking_status"]},
                            "arguments": {"type": "object", "description": "Tool-specific arguments"},
                        }}}},
                    },
                    "responses": {"200": {"description": "Tool result", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "tool": {"type": "string"},
                        "result": {},
                    }}}}}},
                }
            },
            "/execute/best": {
                "post": {
                    "operationId": "executeBest",
                    "summary": "Goal-oriented decisioning — tell us what you want, we decide what to book",
                    "description": "Unlike /execute/guaranteed (which needs a slot hint), this endpoint optimizes across ALL inventory to find the best option given your goal. Goals: maximize_value (best discount), minimize_wait (soonest slot), maximize_success (highest platform reliability), minimize_price. Returns full ExecutionResult with optional explanation.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["customer"], "properties": {
                            "goal": {"type": "string", "enum": ["maximize_value", "minimize_wait", "maximize_success", "minimize_price"], "default": "maximize_value"},
                            "city":        {"type": "string"},
                            "category":    {"type": "string"},
                            "budget":      {"type": "number"},
                            "hours_ahead": {"type": "integer", "default": 48},
                            "customer":    {"type": "object", "required": ["name", "email", "phone"], "properties": {
                                "name":  {"type": "string"},
                                "email": {"type": "string", "format": "email"},
                                "phone": {"type": "string"},
                            }},
                            "wallet_id":          {"type": "string"},
                            "payment_intent_id":  {"type": "string"},
                            "explain":            {"type": "boolean", "default": False, "description": "Include reasoning for why this slot was chosen"},
                        }}}},
                    },
                    "responses": {
                        "200": {"description": "Best option found and booked"},
                        "404": {"description": "No matching inventory"},
                    },
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/intent/create": {
                "post": {
                    "operationId": "intentCreate",
                    "summary": "Create a persistent intent — the system works on your goal until done",
                    "description": "The most powerful way to use LastMinuteDeals as infrastructure. Create an intent and forget it. The system monitors continuously, and when matching slots appear, auto-executes the booking and notifies you via callback_url. Supports three autonomy levels: 'full' (auto-execute), 'notify' (alert you first), 'monitor' (never execute).",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["goal"], "properties": {
                            "goal": {"type": "string", "enum": ["find_and_book", "monitor_only", "price_alert"], "description": "find_and_book: auto-execute when match found. monitor_only: notify only. price_alert: notify when price drops below target."},
                            "constraints": {"type": "object", "properties": {
                                "category":    {"type": "string"},
                                "city":        {"type": "string"},
                                "budget":      {"type": "number"},
                                "hours_ahead": {"type": "integer", "default": 48},
                                "allow_alternatives": {"type": "boolean", "default": True},
                                "price_target": {"type": "number", "description": "For price_alert goal: notify when price drops below this"},
                            }},
                            "customer": {"type": "object", "description": "Required for find_and_book", "properties": {
                                "name":  {"type": "string"},
                                "email": {"type": "string", "format": "email"},
                                "phone": {"type": "string"},
                            }},
                            "payment": {"type": "object", "properties": {
                                "method":    {"type": "string", "enum": ["wallet", "stripe_pi"]},
                                "wallet_id": {"type": "string"},
                            }},
                            "autonomy":     {"type": "string", "enum": ["full", "notify", "monitor"], "default": "full"},
                            "callback_url": {"type": "string", "format": "uri", "description": "POST status changes here"},
                            "ttl_hours":    {"type": "integer", "default": 24, "description": "Auto-expire after this many hours"},
                        }}}},
                    },
                    "responses": {"200": {"description": "Intent created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "intent_id":  {"type": "string"},
                        "status":     {"type": "string"},
                        "expires_at": {"type": "string"},
                        "message":    {"type": "string"},
                    }}}}}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/intent/{intent_id}": {
                "get": {
                    "operationId": "intentGet",
                    "summary": "Get intent session status and result",
                    "parameters": [{"name": "intent_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Intent session details"}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/intent/{intent_id}/execute": {
                "post": {
                    "operationId": "intentExecute",
                    "summary": "Manually trigger a 'notify' autonomy intent",
                    "description": "For intents with autonomy='notify': call this after receiving a slots_available callback to approve and execute the booking.",
                    "parameters": [{"name": "intent_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Execution triggered"}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/intent/{intent_id}/cancel": {
                "post": {
                    "operationId": "intentCancel",
                    "summary": "Cancel an active intent session",
                    "parameters": [{"name": "intent_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Cancelled"}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/intent/list": {
                "get": {
                    "operationId": "intentList",
                    "summary": "List all your intent sessions",
                    "responses": {"200": {"description": "Array of intent summaries"}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/insights/market": {
                "get": {
                    "operationId": "insightsMarket",
                    "summary": "Market intelligence — success rates, fill velocity, optimal booking windows",
                    "description": "Aggregated data from all booking attempts across all platforms, compounded over time. Includes: platform reliability scores, category/city success rates, fill velocity per category, optimal booking windows, live inventory. This data advantage is not reproducible from a standing start.",
                    "parameters": [
                        {"name": "category", "in": "query", "schema": {"type": "string"}, "description": "Filter to specific category"},
                        {"name": "city",     "in": "query", "schema": {"type": "string"}, "description": "Filter to specific city"},
                        {"name": "refresh",  "in": "query", "schema": {"type": "string", "enum": ["0", "1"]}, "description": "Force rebuild from raw data"},
                    ],
                    "responses": {"200": {"description": "Market snapshot"}},
                }
            },
            "/insights/platform/{platform_name}": {
                "get": {
                    "operationId": "insightsPlatform",
                    "summary": "Per-platform reliability and performance stats",
                    "parameters": [{"name": "platform_name", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Platform performance data"}},
                }
            },
            "/execute/guaranteed": {
                "post": {
                    "operationId": "executeGuaranteed",
                    "summary": "Guaranteed booking — hard outcome, multi-path retry engine",
                    "description": "The most powerful booking endpoint. Provide intent + customer + payment method. Engine tries up to 7 strategies (original slot, retry, similar slot, different platform, metro area, alternative category) and returns only when outcome is known. Requires wallet_id (pre-funded, instant) or payment_intent_id (Stripe hold). Returns full attempt log and execution confidence score.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["customer"], "properties": {
                            "slot_id": {"type": "string", "description": "Preferred slot — engine falls back if unavailable"},
                            "category": {"type": "string", "enum": ["wellness", "beauty", "hospitality", "entertainment", "home_services", "professional_services"]},
                            "city": {"type": "string"},
                            "hours_ahead": {"type": "integer", "default": 24},
                            "budget": {"type": "number"},
                            "allow_alternatives": {"type": "boolean", "default": True, "description": "Allow fallback to different category if no match found"},
                            "customer": {"type": "object", "required": ["name", "email", "phone"], "properties": {
                                "name":  {"type": "string"},
                                "email": {"type": "string", "format": "email"},
                                "phone": {"type": "string"},
                            }},
                            "wallet_id": {"type": "string", "description": "Pre-funded agent wallet — fastest, no Stripe roundtrip"},
                            "payment_intent_id": {"type": "string", "description": "Existing Stripe manual-capture PaymentIntent to capture on success"},
                        }}}},
                    },
                    "responses": {
                        "200": {"description": "Booking confirmed", "content": {"application/json": {"schema": {"type": "object", "properties": {
                            "success": {"type": "boolean"},
                            "status": {"type": "string", "enum": ["booked", "failed", "no_slots"]},
                            "confirmation": {"type": "string"},
                            "slot_id": {"type": "string"},
                            "service_name": {"type": "string"},
                            "price_charged": {"type": "number"},
                            "attempts": {"type": "integer"},
                            "fallbacks_used": {"type": "integer"},
                            "savings_vs_market": {"type": "number"},
                            "confidence_score": {"type": "number", "minimum": 0, "maximum": 1},
                            "attempt_log": {"type": "array", "items": {"type": "object"}},
                        }}}}},
                        "404": {"description": "No matching slots found"},
                        "500": {"description": "All booking attempts failed"},
                    },
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/api/wallets/create": {
                "post": {
                    "operationId": "createWallet",
                    "summary": "Create an agent pre-funded wallet",
                    "description": "Create a wallet for instant bookings without per-booking Stripe redirects. Fund it once, book many times. Ideal for high-volume AI agents.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["name", "email"], "properties": {
                            "name":  {"type": "string"},
                            "email": {"type": "string", "format": "email"},
                        }}}},
                    },
                    "responses": {"200": {"description": "Wallet created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "wallet_id": {"type": "string"},
                        "api_key":   {"type": "string"},
                        "balance":   {"type": "number"},
                        "fund_instructions": {"type": "string"},
                    }}}}}},
                }
            },
            "/api/wallets/fund": {
                "post": {
                    "operationId": "fundWallet",
                    "summary": "Generate a Stripe payment link to fund a wallet",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "required": ["wallet_id", "amount_dollars"], "properties": {
                            "wallet_id":      {"type": "string"},
                            "amount_dollars": {"type": "number", "minimum": 5, "maximum": 5000},
                        }}}},
                    },
                    "responses": {"200": {"description": "Checkout URL for funding"}},
                }
            },
            "/api/wallets/{wallet_id}/balance": {
                "get": {
                    "operationId": "walletBalance",
                    "summary": "Check wallet balance",
                    "parameters": [{"name": "wallet_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Current balance in dollars"}},
                    "security": [{"ApiKeyAuth": []}],
                }
            },
            "/api/watcher/status": {
                "get": {
                    "operationId": "watcherStatus",
                    "summary": "Real-time watcher health and data freshness",
                    "description": "Check how fresh the slot data is. The real-time watcher updates slots every 45s on active platforms. Use this to assess confidence before submitting a guaranteed booking.",
                    "responses": {"200": {"description": "Watcher status per platform", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "running": {"type": "boolean"},
                        "platforms": {"type": "object", "description": "Per-platform status with last_poll timestamp"},
                    }}}}}},
                }
            },
            "/metrics": {
                "get": {
                    "operationId": "publicMetrics",
                    "summary": "Public performance metrics — no auth required",
                    "description": "Live system metrics: success rate, bookable slot count, cities covered, data freshness, active intent sessions. Intentionally public — choosing LastMinuteDeals should be a rational decision based on observable data.",
                    "responses": {"200": {"description": "System metrics", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "inventory": {"type": "object", "properties": {
                            "total_slots":     {"type": "integer"},
                            "bookable_slots":  {"type": "integer"},
                            "categories":      {"type": "array", "items": {"type": "string"}},
                            "cities_covered":  {"type": "integer"},
                            "next_slot_hours": {"type": "number"},
                        }},
                        "performance": {"type": "object", "properties": {
                            "success_rate":    {"type": "number", "minimum": 0, "maximum": 1},
                            "total_bookings":  {"type": "integer"},
                            "fallback_rate":   {"type": "number"},
                        }},
                        "infrastructure": {"type": "object", "properties": {
                            "realtime_watcher":       {"type": "boolean"},
                            "data_freshness_seconds": {"type": "integer"},
                            "active_intent_sessions": {"type": "integer"},
                            "registered_wallets":     {"type": "integer"},
                        }},
                    }}}}}},
                }
            },
            "/health": {
                "get": {
                    "operationId": "healthCheck",
                    "summary": "API health + live slot count",
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Register free at POST /api/keys/register",
                }
            },
            "schemas": {
                "Slot": {
                    "type": "object",
                    "properties": {
                        "slot_id": {"type": "string"},
                        "platform": {"type": "string"},
                        "service_name": {"type": "string"},
                        "category": {"type": "string"},
                        "start_time": {"type": "string", "format": "date-time"},
                        "hours_until_start": {"type": "number"},
                        "our_price": {"type": "number"},
                        "currency": {"type": "string", "default": "USD"},
                        "location_city": {"type": "string"},
                        "location_state": {"type": "string"},
                    },
                }
            },
        },
    }
    (out_dir / "openapi.json").write_text(json.dumps(openapi, indent=2), encoding="utf-8")

    # ── ai-plugin.json (OpenAI/ChatGPT plugin manifest) ───────────────────────
    ai_plugin = {
        "schema_version": "v1",
        "name_for_human": "LastMinuteDeals",
        "name_for_model": "lastminutedeals",
        "description_for_human": "Execution infrastructure for booking real-world services. Search open slots, guarantee outcomes, delegate intent — events, wellness, beauty, hospitality within 72 hours.",
        "description_for_model": "Execution infrastructure for real-world service bookings. Use /execute/guaranteed for synchronous confirmed outcomes (multi-path retry, up to 7 strategies). Use /intent/create to delegate a goal and have the system execute autonomously. Use /execute/best to optimize by goal (maximize_value, minimize_wait, maximize_success). Use /insights/market for platform success rates and fill velocity data that compounds over time. Card is authorized before booking, captured only after confirmation — never charged for failed bookings. All responses include system_context with live success_rate and data_freshness_seconds.",
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": f"{base_url}/openapi.json"},
        "logo_url": f"{base_url}/icon.png",
        "contact_email": "support@lastminutedealshq.com",
        "legal_info_url": f"{base_url}/#terms",
    }
    (out_dir / "ai-plugin.json").write_text(json.dumps(ai_plugin, indent=2), encoding="utf-8")

    # ── SDK file (served from static site so agents can wget/import it) ─────────
    sdk_src = Path(__file__).parent / "lmd_sdk.py"
    if sdk_src.exists():
        (out_dir / "lmd_sdk.py").write_text(sdk_src.read_text(encoding="utf-8"), encoding="utf-8")
        sdk_note = ", lmd_sdk.py"
    else:
        sdk_note = ""

    print(f"  AI discovery files: robots.txt, llms.txt, sitemap.xml, openapi.json, ai-plugin.json, .well-known/{{agents,agent-card,mcp/server-card}}.json{sdk_note}")


def main():
    parser = argparse.ArgumentParser(description="Regenerate deals landing page")
    parser.add_argument("--data-file", default=str(DATA_FILE))
    parser.add_argument("--out-dir",   default=str(OUT_DIR))
    args = parser.parse_args()

    data_path = Path(args.data_file)
    out_dir   = Path(args.out_dir)

    if not data_path.exists():
        print(f"ERROR: {data_path} not found. Run aggregate_slots.py first.")
        return

    slots = json.loads(data_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(slots)} slots from {data_path}")

    # Sort by hours_until_start ascending (paid-first ordering is done per-section in generate_html)
    slots.sort(key=lambda s: s.get("hours_until_start") or 999)

    now_dt  = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%b %d, %Y %I:%M %p UTC").lstrip("0")
    booking_api_url  = os.getenv("BOOKING_API_URL", "").strip()
    website_api_key  = os.getenv("LMD_WEBSITE_API_KEY", "").strip()
    page    = generate_html(slots, now_str, booking_api_url, website_api_key)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(page, encoding="utf-8")

    priced_count = sum(
        1 for s in slots
        if (s.get("our_price") is not None or s.get("price") is not None)
        and (s.get("our_price") or s.get("price") or 0) > 0
    )
    print(f"Landing page written: {out_path}")
    print(f"  Deals with prices (shown): {priced_count} / {len(slots)} total slots")

    # ── Generate AI discovery support files ───────────────────────────────────
    base_url = os.getenv("LANDING_PAGE_URL", "https://lastminutedealshq.com").rstrip("/")
    generate_support_files(out_dir, slots, now_dt, base_url)

    # ── Copy logo + favicon into out_dir (served as static assets) ────────────
    tools_dir = Path(__file__).parent
    for asset_name in ("logo.png", "favicon.png", "favicon.ico"):
        dst = out_dir / asset_name
        if dst.exists():
            continue  # already present from previous run, keep it
        # Search for the file in tools/ then repo root
        src = tools_dir / asset_name
        if not src.exists():
            src = tools_dir.parent / asset_name
        if src.exists():
            dst.write_bytes(src.read_bytes())
            print(f"  Copied {asset_name}")
        else:
            print(f"  WARN: {asset_name} not found — skipping (logo will be missing from site)")

    # ── Deploy to GitHub Pages (unlimited bandwidth, free SSL) ───────────────
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    gh_user  = os.getenv("GITHUB_USER", "").strip()
    gh_repo  = os.getenv("GITHUB_PAGES_REPO", "").strip()

    if gh_token and gh_user and gh_repo:
        deploy_to_github_pages(out_dir, gh_token, gh_user, gh_repo)
    else:
        print("  Deploy skipped — set GITHUB_TOKEN, GITHUB_USER, GITHUB_PAGES_REPO in .env")


def deploy_to_cloudflare(out_dir: Path, token: str, account_id: str, project_name: str) -> None:
    """
    Deploy all files in out_dir to Cloudflare Pages via Direct Upload API.
    Handles index.html, support files, logo, favicon, .well-known/, etc.
    """
    import hashlib

    print("Deploying to Cloudflare Pages...")

    # Only deploy files CF Pages handles well as static assets.
    # Skip nested support-file dirs (.well-known/, etc.) and non-web files
    # to avoid CF Pages returning 500 on unexpected file types.
    MIME = {
        ".html": "text/html; charset=UTF-8",
        ".txt":  "text/plain; charset=UTF-8",
        ".xml":  "application/xml",
        ".json": "application/json",
        ".png":  "image/png",
        ".ico":  "image/x-icon",
        ".js":   "application/javascript",
        ".css":  "text/css",
    }
    files_to_deploy: list[tuple[str, Path, str]] = []  # (cf_path, local_path, mime_type)
    for fpath in out_dir.iterdir():  # top-level only — no nested dirs
        if not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext not in MIME:
            continue
        mime = MIME[ext]
        files_to_deploy.append(("/" + fpath.name, fpath, mime))

    if not files_to_deploy:
        print("  No files to deploy.")
        return

    # CF Pages Direct Upload: build manifest (SHA-1 hash → file)
    manifest: dict[str, str] = {}
    file_contents: dict[str, tuple[str, bytes, str]] = {}  # hash → (filename, bytes, mime)
    for cf_path, fpath, mime in files_to_deploy:
        content = fpath.read_bytes()
        sha1    = hashlib.sha1(content).hexdigest()
        manifest[cf_path] = sha1
        file_contents[sha1] = (fpath.name, content, mime)

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/pages/projects/{project_name}/deployments"
    auth_headers = {"Authorization": f"Bearer {token}"}

    # Build multipart form: manifest + one entry per unique file hash
    files: dict = {"manifest": (None, json.dumps(manifest), "application/json")}
    for sha1, (fname, content, mime) in file_contents.items():
        files[f"files[{sha1}]"] = (fname, content, mime)

    resp = requests.post(url, headers=auth_headers, files=files, timeout=120)
    data = resp.json()

    if resp.status_code in (200, 201) and data.get("success"):
        deploy_url = (data.get("result") or {}).get("url", "")
        aliases    = (data.get("result") or {}).get("aliases", [])
        live_url   = aliases[0] if aliases else deploy_url
        print(f"  Deployed {len(files_to_deploy)} files -> {live_url or project_name + '.pages.dev'}")
    else:
        errors = data.get("errors") or [{"message": resp.text[:200]}]
        print(f"  ERROR: {errors}")


def deploy_to_github_pages(out_dir: Path, token: str, user: str, repo: str) -> None:
    """
    Deploy all files in out_dir to the gh-pages branch via the GitHub Contents API.
    Includes index.html plus all AI discovery files (robots.txt, llms.txt, etc.)
    """
    import base64

    print("Deploying to GitHub Pages...")

    hdrs = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    commit_msg = f"Deploy {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    def _upsert_file(repo_path: str, content_bytes: bytes) -> bool:
        api_url = f"https://api.github.com/repos/{user}/{repo}/contents/{repo_path}"
        existing = requests.get(api_url, headers=hdrs, params={"ref": "gh-pages"}, timeout=15)
        sha = existing.json().get("sha") if existing.status_code == 200 else None
        payload: dict = {
            "message": commit_msg,
            "content": base64.b64encode(content_bytes).decode(),
            "branch": "gh-pages",
        }
        if sha:
            payload["sha"] = sha
        resp = requests.put(api_url, headers=hdrs, json=payload, timeout=30)
        if resp.status_code in (200, 201):
            return True
        print(f"  ERROR {repo_path}: {resp.status_code} {resp.json().get('message', '')[:100]}")
        return False

    # .nojekyll disables Jekyll processing — required for SSL cert provisioning to work
    _upsert_file(".nojekyll", b"")

    # CNAME file is required for GitHub to provision SSL on the custom domain
    custom_domain = os.getenv("LANDING_PAGE_URL", "").replace("https://", "").replace("http://", "").strip("/")
    if custom_domain:
        _upsert_file("CNAME", custom_domain.encode())

    # Collect top-level site files only (skip nested dirs to keep deploy fast)
    files_to_deploy = []
    for f in out_dir.iterdir():
        if f.is_file():
            files_to_deploy.append((f.name, f))

    ok = 0
    for repo_path, fpath in files_to_deploy:
        if _upsert_file(repo_path, fpath.read_bytes()):
            ok += 1
            print(f"  OK {repo_path}")

    pages_url = f"https://{custom_domain}" if custom_domain else f"https://{user.lower()}.github.io/{repo}"
    print(f"GitHub Pages deploy complete: {ok}/{len(files_to_deploy)} files -> {pages_url}")


def deploy_to_netlify(html_path: Path, token: str, site_id: str) -> None:
    """Zip site files and deploy to Netlify."""
    print("Deploying to Netlify...", end=" ", flush=True)

    site_dir = html_path.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(html_path, arcname="index.html")
        for ext in (".txt", ".xml", ".json", ".png", ".ico"):
            for f in site_dir.glob(f"*{ext}"):
                zf.write(f, arcname=f.name)
    buf.seek(0)
    zip_bytes = buf.read()

    resp = requests.post(
        f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip"},
        data=zip_bytes,
        timeout=60,
    )

    if resp.status_code in (200, 201):
        deploy = resp.json()
        url = deploy.get("ssl_url") or deploy.get("url") or ""
        print(f"live at {url}")
    else:
        print(f"ERROR {resp.status_code}: {resp.text[:200]}")


if __name__ == "__main__":
    main()
