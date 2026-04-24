"""
send_booking_email.py — Transactional email system for LastMinuteDeals bookings.

Sends four types of emails over SMTP (with SendGrid HTTP fallback):
  - checkout_created   : Checkout session created — customer must click link to pay
  - booking_initiated  : Payment hold placed, booking in progress
  - booking_confirmed  : Booking fully confirmed on source platform
  - booking_failed     : Booking could not complete, card not charged

Credentials are read from .env:
  EMAIL_FROM        — sender address (e.g. bookings@lastminutedealshq.com)
  EMAIL_FROM_NAME   — display name (e.g. "LastMinuteDeals")
  SMTP_HOST         — SMTP server hostname
  SMTP_PORT         — SMTP port (default 587)
  SMTP_USER         — SMTP username
  SMTP_PASSWORD     — SMTP password
  SMTP_USE_TLS      — "true" or "false" (default "true")
  SENDGRID_API_KEY  — (optional) SendGrid fallback via HTTP API

Usage (programmatic):
    from tools.send_booking_email import send_booking_email
    ok = send_booking_email(
        email_type="booking_confirmed",
        customer_email="jane@example.com",
        customer_name="Jane Smith",
        slot={...},
        confirmation_number="LMD-4892",
    )

Usage (CLI test):
    python tools/send_booking_email.py --test-to jane@example.com
"""

import argparse
import json
import os
import smtplib
import sys
import textwrap
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# ── Load .env ─────────────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent.parent / ".env"


def _load_env() -> None:
    """Load .env file into os.environ if keys not already set."""
    if not _ENV_PATH.exists():
        return
    with open(_ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env()

# ── Brand constants ────────────────────────────────────────────────────────────
BRAND_NAVY    = "#0f172a"
BRAND_BLUE    = "#38bdf8"
BRAND_BLUE_DK = "#0ea5e9"   # darker sky-blue for hover / borders
BRAND_BG      = "#f8fafc"
BRAND_WHITE   = "#ffffff"
BRAND_GRAY    = "#64748b"
BRAND_DARK    = "#1e293b"
BRAND_SUCCESS = "#10b981"
BRAND_WARN    = "#f59e0b"
BRAND_ERROR   = "#ef4444"

FONT_STACK = "'Inter', 'Helvetica Neue', Arial, sans-serif"


# ══════════════════════════════════════════════════════════════════════════════
# HTML helpers
# ══════════════════════════════════════════════════════════════════════════════

def _html_shell(preheader: str, body_html: str) -> str:
    """Wrap body HTML in the standard branded email shell."""
    brand_name = os.environ.get("EMAIL_FROM_NAME", "LastMinuteDeals")
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="X-UA-Compatible" content="IE=edge" />
  <title>{brand_name}</title>
  <!--[if mso]>
  <noscript>
    <xml><o:OfficeDocumentSettings>
      <o:PixelsPerInch>96</o:PixelsPerInch>
    </o:OfficeDocumentSettings></xml>
  </noscript>
  <![endif]-->
  <style>
    /* Reset */
    body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; border-collapse: collapse; }}
    img {{ -ms-interpolation-mode: bicubic; border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; }}
    /* Client-specific */
    .ReadMsgBody {{ width: 100%; }} .ExternalClass {{ width: 100%; }}
    .ExternalClass, .ExternalClass p, .ExternalClass span, .ExternalClass font,
    .ExternalClass td, .ExternalClass div {{ line-height: 100%; }}
    /* Prevent auto-links in Apple Mail */
    a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important; font-size: inherit !important; font-family: inherit !important; font-weight: inherit !important; line-height: inherit !important; }}
    /* Mobile */
    @media screen and (max-width: 600px) {{
      .email-container {{ width: 100% !important; max-width: 100% !important; }}
      .fluid {{ max-width: 100% !important; height: auto !important; margin: auto !important; }}
      .stack-on-mobile {{ display: block !important; width: 100% !important; }}
      .center-on-mobile {{ text-align: center !important; }}
      .pad-mobile {{ padding: 24px 16px !important; }}
      .hide-mobile {{ display: none !important; }}
    }}
  </style>
</head>
<body style="margin:0; padding:0; background-color:{BRAND_BG}; font-family:{FONT_STACK};">

  <!-- Preheader (hidden) -->
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all; font-size:1px; color:{BRAND_BG}; line-height:1px;">
    {preheader}&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;&nbsp;&#847;
  </div>

  <!-- Outer wrapper -->
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color:{BRAND_BG};">
    <tr>
      <td align="center" style="padding: 32px 16px;">

        <!-- Email container -->
        <table role="presentation" class="email-container" cellspacing="0" cellpadding="0" border="0"
               style="max-width:600px; width:100%; background-color:{BRAND_WHITE}; border-radius:12px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,0.08);">

          <!-- Header bar -->
          <tr>
            <td style="background-color:{BRAND_NAVY}; padding:28px 40px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td>
                    <span style="font-family:{FONT_STACK}; font-size:22px; font-weight:700; color:{BRAND_WHITE}; letter-spacing:-0.3px;">{brand_name}</span>
                    <span style="font-family:{FONT_STACK}; font-size:12px; font-weight:500; color:{BRAND_BLUE}; display:block; margin-top:2px; letter-spacing:0.5px; text-transform:uppercase;">Last-Minute Experiences</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          {body_html}

          <!-- Footer -->
          <tr>
            <td style="background-color:{BRAND_NAVY}; padding:28px 40px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="text-align:center;">
                    <p style="margin:0 0 8px; font-family:{FONT_STACK}; font-size:13px; color:{BRAND_BLUE}; font-weight:600;">{brand_name}</p>
                    <p style="margin:0 0 12px; font-family:{FONT_STACK}; font-size:12px; color:#94a3b8; line-height:1.6;">
                      Questions? Reply to this email or contact us at
                      <a href="mailto:{os.environ.get('EMAIL_FROM', 'support@lastminutedealshq.com')}"
                         style="color:{BRAND_BLUE}; text-decoration:none;">{os.environ.get('EMAIL_FROM', 'support@lastminutedealshq.com')}</a>
                    </p>
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:11px; color:#475569;">
                      You received this email because you made a booking through {brand_name}.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
        <!-- /Email container -->

      </td>
    </tr>
  </table>
  <!-- /Outer wrapper -->

</body>
</html>"""


def _divider() -> str:
    return f'<tr><td style="padding:0 40px;"><div style="height:1px; background-color:#e2e8f0;"></div></td></tr>'


def _detail_row(label: str, value: str) -> str:
    return f"""
      <tr>
        <td style="font-family:{FONT_STACK}; font-size:13px; font-weight:600; color:{BRAND_GRAY};
                   text-transform:uppercase; letter-spacing:0.5px; padding:8px 0 2px;">{label}</td>
      </tr>
      <tr>
        <td style="font-family:{FONT_STACK}; font-size:16px; font-weight:500; color:{BRAND_DARK};
                   padding:0 0 16px;">{value}</td>
      </tr>"""


def _cta_button(text: str, url: str, bg: str = None) -> str:
    bg = bg or BRAND_BLUE
    return f"""
      <table role="presentation" cellspacing="0" cellpadding="0" border="0">
        <tr>
          <td style="border-radius:8px; background-color:{bg};">
            <a href="{url}"
               style="display:inline-block; padding:14px 32px; font-family:{FONT_STACK}; font-size:15px;
                      font-weight:700; color:{BRAND_NAVY}; text-decoration:none; letter-spacing:-0.1px;
                      border-radius:8px;"
               target="_blank">{text}</a>
          </td>
        </tr>
      </table>"""


# ══════════════════════════════════════════════════════════════════════════════
# Slot field helpers
# ══════════════════════════════════════════════════════════════════════════════

def _format_dt(iso: str) -> str:
    """Parse ISO 8601 → human-readable: 'Saturday, March 29 at 2:00 PM'"""
    if not iso:
        return "—"
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%A, %B %-d at %-I:%M %p")
    except Exception:
        # Windows doesn't support %-d / %-I, fall back
        try:
            return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
        except Exception:
            return iso


def _format_price(price, currency: str = "USD") -> str:
    if price is None:
        return "—"
    try:
        return f"${float(price):.2f}"
    except Exception:
        return str(price)


def _gcal_url(slot: dict) -> str:
    """Build a Google Calendar 'add event' URL from a slot dict."""
    title   = quote(slot.get("service_name", "My Booking"))
    city    = slot.get("location_city", "")
    state   = slot.get("location_state", "")
    loc     = quote(f"{city}, {state}".strip(", "))
    details = quote(f"Booked via LastMinuteDeals. Business: {slot.get('business_name', '')}")

    start_iso = slot.get("start_time", "")
    try:
        if start_iso.endswith("Z"):
            start_iso = start_iso[:-1] + "+00:00"
        dt_start = datetime.fromisoformat(start_iso)
        # Google Calendar format: YYYYMMDDTHHmmssZ
        gcal_start = dt_start.strftime("%Y%m%dT%H%M%SZ")
        # Default: assume 1-hour duration if end_time absent
        end_iso = slot.get("end_time", "")
        if end_iso:
            if end_iso.endswith("Z"):
                end_iso = end_iso[:-1] + "+00:00"
            dt_end = datetime.fromisoformat(end_iso)
        else:
            from datetime import timedelta
            dt_end = dt_start + timedelta(hours=1)
        gcal_end = dt_end.strftime("%Y%m%dT%H%M%SZ")
        dates = f"{gcal_start}/{gcal_end}"
    except Exception:
        dates = ""

    return (
        f"https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={title}&dates={dates}&details={details}&location={loc}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Email body builders
# ══════════════════════════════════════════════════════════════════════════════


def _build_checkout_created_html(customer_name: str, slot: dict) -> tuple[str, str]:
    """
    Email sent immediately when book_slot is called — before the customer has paid.
    Primary CTA: checkout_url (Stripe hosted checkout page).
    The customer must click this link and complete payment to confirm their booking.
    """
    service      = slot.get("service_name", "Your Experience")
    city         = slot.get("location_city", "")
    state        = slot.get("location_state", "")
    location     = f"{city}, {state}".strip(", ") or "—"
    date_str     = _format_dt(slot.get("start_time", ""))
    quantity     = int(slot.get("quantity") or 1)
    price_pp     = slot.get("our_price") or slot.get("price")
    total_price  = float(price_pp or 0) * quantity
    currency     = slot.get("currency", "USD")
    checkout_url = slot.get("checkout_url", "#")
    first_name   = customer_name.split()[0] if customer_name else "there"

    if quantity > 1:
        price_display = f"${total_price:.2f} ({quantity} × {_format_price(price_pp, currency)})"
    else:
        price_display = _format_price(price_pp, currency)

    html_body = f"""
          <!-- Hero -->
          <tr>
            <td class="pad-mobile" style="padding:40px 40px 32px;">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px; font-weight:700;
                         color:{BRAND_BLUE}; text-transform:uppercase; letter-spacing:1px;">Action Required</p>
              <h1 style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:28px; font-weight:800;
                          color:{BRAND_NAVY}; line-height:1.2; letter-spacing:-0.5px;">
                Complete your booking
              </h1>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; color:{BRAND_GRAY}; line-height:1.6;">
                Hi {first_name}, your booking for <strong style="color:{BRAND_DARK};">{service}</strong>
                is reserved — click below to pay and lock it in. No card entered yet.
              </p>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td style="padding:0 40px 32px; text-align:center;">
              {_cta_button("Complete Booking &rarr;", checkout_url)}
              <p style="margin:16px 0 0; font-family:{FONT_STACK}; font-size:13px; color:{BRAND_WARN}; font-weight:600;">
                &#x23F0; This link expires in 24 hours
              </p>
            </td>
          </tr>

          {_divider()}

          <!-- Booking details -->
          <tr>
            <td style="padding:32px 40px 8px;">
              <p style="margin:0 0 20px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">Booking Summary</p>
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                {_detail_row("Experience", service)}
                {_detail_row("Location",   location)}
                {_detail_row("Date &amp; Time", date_str)}
                {_detail_row("Guests",     str(quantity))}
                {_detail_row("Total",      price_display)}
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- How it works -->
          <tr>
            <td style="padding:32px 40px;">
              <p style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">How It Works</p>
              <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="vertical-align:top; padding-right:12px; padding-bottom:14px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">1</div>
                  </td>
                  <td style="padding-bottom:14px;">
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>Click the button above</strong> to open our secure Stripe checkout page.
                    </p>
                  </td>
                </tr>
                <tr>
                  <td style="vertical-align:top; padding-right:12px; padding-bottom:14px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">2</div>
                  </td>
                  <td style="padding-bottom:14px;">
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>Enter your payment details</strong> — we use Stripe for secure processing.
                      Your card is not charged until the booking is confirmed.
                    </p>
                  </td>
                </tr>
                <tr>
                  <td style="vertical-align:top; padding-right:12px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">3</div>
                  </td>
                  <td>
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>You'll receive a confirmation email</strong> within minutes with your booking reference.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
    """

    plain = textwrap.dedent(f"""\
        Hi {first_name},

        Your booking is reserved — complete payment to confirm it.

        COMPLETE YOUR BOOKING: {checkout_url}

        ⚠ This link expires in 24 hours.

        Booking Summary
        ---------------
        Experience : {service}
        Location   : {location}
        Date       : {date_str}
        Guests     : {quantity}
        Total      : {price_display}

        How it works:
        1. Click the link above to open our Stripe checkout page.
        2. Enter your card details (not charged until confirmed).
        3. You'll receive a confirmation email once we've confirmed your spot.

        Questions? Reply to this email.
        — Last Minute Deals
    """)

    return html_body, plain

def _build_initiated_html(customer_name: str, slot: dict) -> tuple[str, str]:
    """Returns (html_body_rows, plain_text)"""
    service    = slot.get("service_name", "Your Service")
    city       = slot.get("location_city", "")
    state      = slot.get("location_state", "")
    location   = f"{city}, {state}".strip(", ") or "—"
    date_str   = _format_dt(slot.get("start_time", ""))
    price      = _format_price(slot.get("our_price") or slot.get("price"))
    first_name = customer_name.split()[0] if customer_name else "there"

    html_body = f"""
          <!-- Hero -->
          <tr>
            <td class="pad-mobile" style="padding:40px 40px 32px;">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px; font-weight:700;
                         color:{BRAND_BLUE}; text-transform:uppercase; letter-spacing:1px;">Booking In Progress</p>
              <h1 style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:28px; font-weight:800;
                          color:{BRAND_NAVY}; line-height:1.2; letter-spacing:-0.5px;">
                We're confirming your booking
              </h1>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; color:{BRAND_GRAY}; line-height:1.6;">
                Hi {first_name}, great choice! We're securing your spot at <strong style="color:{BRAND_DARK};">{service}</strong>.
                You'll receive a confirmation email within the next few minutes.
              </p>
            </td>
          </tr>

          <!-- Trust badge -->
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="background-color:#f0fdf4; border:1.5px solid #86efac; border-radius:10px; padding:18px 24px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        <td style="width:32px; vertical-align:top; padding-right:14px;">
                          <div style="width:32px; height:32px; background-color:#22c55e; border-radius:50%;
                                      text-align:center; line-height:32px; font-size:18px;">&#x1F512;</div>
                        </td>
                        <td>
                          <p style="margin:0 0 4px; font-family:{FONT_STACK}; font-size:14px; font-weight:700; color:#15803d;">
                            Your card has been reserved — not charged yet
                          </p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:13px; color:#166534; line-height:1.5;">
                            We've placed a temporary hold while we confirm your booking. You will only be charged
                            once the booking is fully confirmed. If anything goes wrong, the hold is released automatically.
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- Booking details -->
          <tr>
            <td style="padding:32px 40px 8px;">
              <p style="margin:0 0 20px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">Booking Details</p>
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                {_detail_row("Service", service)}
                {_detail_row("Location", location)}
                {_detail_row("Date &amp; Time", date_str)}
                {_detail_row("Amount Reserved", price)}
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- What happens next -->
          <tr>
            <td style="padding:32px 40px;">
              <p style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">What Happens Next</p>
              <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="vertical-align:top; padding-right:12px; padding-bottom:14px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">1</div>
                  </td>
                  <td style="padding-bottom:14px;">
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>We confirm your spot</strong> directly on the booking platform — usually takes 1–3 minutes.
                    </p>
                  </td>
                </tr>
                <tr>
                  <td style="vertical-align:top; padding-right:12px; padding-bottom:14px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">2</div>
                  </td>
                  <td style="padding-bottom:14px;">
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>You get a confirmation email</strong> with your booking number and everything you need to show up.
                    </p>
                  </td>
                </tr>
                <tr>
                  <td style="vertical-align:top; padding-right:12px;">
                    <div style="width:24px; height:24px; background-color:{BRAND_BLUE}; border-radius:50%;
                                text-align:center; line-height:24px; font-family:{FONT_STACK}; font-size:12px;
                                font-weight:700; color:{BRAND_NAVY};">3</div>
                  </td>
                  <td>
                    <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                      <strong>Your card is charged</strong> only after confirmation is complete.
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
    """

    plain = textwrap.dedent(f"""
        BOOKING IN PROGRESS — LastMinuteDeals
        ======================================

        Hi {first_name},

        We're securing your spot at {service}. You'll receive a confirmation
        within the next few minutes.

        ✅ YOUR CARD HAS BEEN RESERVED — NOT CHARGED YET
        We've placed a temporary hold while we confirm your booking.
        You will only be charged once the booking is fully confirmed.

        BOOKING DETAILS
        ---------------
        Service:         {service}
        Location:        {location}
        Date & Time:     {date_str}
        Amount Reserved: {price}

        WHAT HAPPENS NEXT
        -----------------
        1. We confirm your spot directly on the booking platform (1–3 minutes).
        2. You'll get a confirmation email with your booking number.
        3. Your card is charged only after confirmation is complete.

        Questions? Reply to this email.
        — LastMinuteDeals
    """).strip()

    return html_body, plain


def _build_confirmed_html(customer_name: str, slot: dict, confirmation_number: str,
                          cancel_url: str = "") -> tuple[str, str]:
    service      = slot.get("service_name", "Your Service")
    business     = slot.get("business_name", "")
    city         = slot.get("location_city", "")
    state        = slot.get("location_state", "")
    location     = f"{city}, {state}".strip(", ") or "—"
    date_str     = _format_dt(slot.get("start_time", ""))
    price        = _format_price(slot.get("our_price") or slot.get("price"))
    first_name   = customer_name.split()[0] if customer_name else "there"
    conf_display = confirmation_number or "—"
    gcal         = _gcal_url(slot)

    html_body = f"""
          <!-- Hero -->
          <tr>
            <td class="pad-mobile" style="padding:40px 40px 32px;">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px; font-weight:700;
                         color:{BRAND_SUCCESS}; text-transform:uppercase; letter-spacing:1px;">Confirmed ✓</p>
              <h1 style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:28px; font-weight:800;
                          color:{BRAND_NAVY}; line-height:1.2; letter-spacing:-0.5px;">
                You're all set, {first_name}!
              </h1>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; color:{BRAND_GRAY}; line-height:1.6;">
                Your booking for <strong style="color:{BRAND_DARK};">{service}</strong> is confirmed.
                We can't wait for you to experience it. See you there!
              </p>
            </td>
          </tr>

          <!-- Show this email box -->
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="background-color:{BRAND_NAVY}; border-radius:12px; padding:24px 28px;">
                    <p style="margin:0 0 4px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                               color:{BRAND_BLUE}; text-transform:uppercase; letter-spacing:1px;">Show this at arrival</p>
                    <p style="margin:0 0 20px; font-family:{FONT_STACK}; font-size:13px; color:#94a3b8; line-height:1.5;">
                      Present this information when you check in.
                    </p>
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        <td style="padding-bottom:14px; border-bottom:1px solid #1e293b;">
                          <p style="margin:0 0 2px; font-family:{FONT_STACK}; font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px;">Confirmation #</p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:20px; font-weight:800; color:{BRAND_WHITE}; letter-spacing:1px;">{conf_display}</p>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding-top:14px; padding-bottom:14px; border-bottom:1px solid #1e293b;">
                          <p style="margin:0 0 2px; font-family:{FONT_STACK}; font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px;">Service</p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; font-weight:600; color:{BRAND_WHITE};">{service}</p>
                          {f'<p style="margin:2px 0 0; font-family:{FONT_STACK}; font-size:13px; color:#94a3b8;">{business}</p>' if business else ''}
                        </td>
                      </tr>
                      <tr>
                        <td style="padding-top:14px; padding-bottom:14px; border-bottom:1px solid #1e293b;">
                          <p style="margin:0 0 2px; font-family:{FONT_STACK}; font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px;">Date &amp; Time</p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; font-weight:600; color:{BRAND_WHITE};">{date_str}</p>
                        </td>
                      </tr>
                      <tr>
                        <td style="padding-top:14px;">
                          <p style="margin:0 0 2px; font-family:{FONT_STACK}; font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px;">Location</p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; font-weight:600; color:{BRAND_WHITE};">{location}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Amount charged -->
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="background-color:#f0fdf4; border:1.5px solid #86efac; border-radius:10px; padding:16px 20px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        <td>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:13px; color:#15803d;">
                            Amount charged to your card
                          </p>
                        </td>
                        <td style="text-align:right;">
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:20px; font-weight:800; color:#15803d;">{price}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- What to bring -->
          <tr>
            <td style="padding:32px 40px;">
              <p style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">Good to Know</p>
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="padding-bottom:12px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                      <tr>
                        <td style="width:20px; vertical-align:top; padding-right:12px; padding-top:2px;">
                          <div style="width:6px; height:6px; background-color:{BRAND_BLUE}; border-radius:50%; margin-top:5px;"></div>
                        </td>
                        <td>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                            <strong>Arrive 5–10 minutes early</strong> so you have time to check in without rushing.
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding-bottom:12px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                      <tr>
                        <td style="width:20px; vertical-align:top; padding-right:12px; padding-top:2px;">
                          <div style="width:6px; height:6px; background-color:{BRAND_BLUE}; border-radius:50%; margin-top:5px;"></div>
                        </td>
                        <td>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                            <strong>Bring a valid ID</strong> and this confirmation email (on your phone is fine).
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td>
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                      <tr>
                        <td style="width:20px; vertical-align:top; padding-right:12px; padding-top:2px;">
                          <div style="width:6px; height:6px; background-color:{BRAND_BLUE}; border-radius:50%; margin-top:5px;"></div>
                        </td>
                        <td>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_DARK}; line-height:1.5;">
                            <strong>Need to cancel?</strong>
                            {"<a href='" + cancel_url + "' style='color:" + BRAND_BLUE_DK + ";'>Cancel this booking</a> — you'll receive a full refund. Cancellations must be made at least 48 hours before the activity." if cancel_url else "Reply to this email as soon as possible and we'll do our best to help."}
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- Add to calendar CTA -->
          <tr>
            <td style="padding:32px 40px 40px; text-align:center;">
              <p style="margin:0 0 20px; font-family:{FONT_STACK}; font-size:15px; color:{BRAND_GRAY};">
                Don't forget — add it to your calendar.
              </p>
              {_cta_button("Add to Google Calendar", gcal)}
            </td>
          </tr>
    """

    plain = textwrap.dedent(f"""
        BOOKING CONFIRMED — LastMinuteDeals
        ====================================

        Hi {first_name}, you're all set!

        Your booking for {service} is confirmed.

        ── SHOW THIS AT ARRIVAL ────────────────
        Confirmation #: {conf_display}
        Service:        {service}
        {"Business:       " + business if business else ""}
        Date & Time:    {date_str}
        Location:       {location}
        Amount Charged: {price}
        ────────────────────────────────────────

        GOOD TO KNOW
        ------------
        • Arrive 5–10 minutes early to check in.
        • Bring a valid ID and this email (phone is fine).
        • Need to cancel? {"Cancel here: " + cancel_url + " (must be 48+ hours before activity)" if cancel_url else "Reply to this email ASAP."}
        • Cancellation policy: Refunds are not available within 48 hours of the scheduled activity.

        ADD TO CALENDAR
        ---------------
        Google Calendar: {gcal}

        Questions? Reply to this email.
        — LastMinuteDeals
    """).strip()

    return html_body, plain


def _build_failed_html(customer_name: str, slot: dict, error_reason: str) -> tuple[str, str]:
    service    = slot.get("service_name", "Your Service")
    city       = slot.get("location_city", "")
    state      = slot.get("location_state", "")
    location   = f"{city}, {state}".strip(", ") or "—"
    date_str   = _format_dt(slot.get("start_time", ""))
    first_name = customer_name.split()[0] if customer_name else "there"
    reason_text = error_reason or "The slot was no longer available when we tried to confirm it."
    brand_name = os.environ.get("EMAIL_FROM_NAME", "LastMinuteDeals")
    # Use booking_url only if it's a real HTTP link — OCTO slots store a JSON blob here
    _raw_burl  = slot.get("booking_url", "")
    retry_url  = _raw_burl if isinstance(_raw_burl, str) and _raw_burl.startswith("http") else "https://lastminutedealshq.com"

    html_body = f"""
          <!-- Hero -->
          <tr>
            <td class="pad-mobile" style="padding:40px 40px 32px;">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px; font-weight:700;
                         color:{BRAND_WARN}; text-transform:uppercase; letter-spacing:1px;">Booking Update</p>
              <h1 style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:28px; font-weight:800;
                          color:{BRAND_NAVY}; line-height:1.2; letter-spacing:-0.5px;">
                We weren't able to complete your booking
              </h1>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; color:{BRAND_GRAY}; line-height:1.6;">
                Hi {first_name}, we're sorry — we ran into an issue securing your spot at
                <strong style="color:{BRAND_DARK};">{service}</strong>.
              </p>
            </td>
          </tr>

          <!-- No charge badge -->
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="background-color:#fff7ed; border:1.5px solid #fdba74; border-radius:10px; padding:18px 24px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        <td style="width:32px; vertical-align:top; padding-right:14px;">
                          <div style="width:32px; height:32px; background-color:{BRAND_WARN}; border-radius:50%;
                                      text-align:center; line-height:34px; font-size:18px;">&#x1F6AB;</div>
                        </td>
                        <td>
                          <p style="margin:0 0 4px; font-family:{FONT_STACK}; font-size:15px; font-weight:800; color:#92400e;">
                            Your card was NOT charged
                          </p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:13px; color:#78350f; line-height:1.5;">
                            Any temporary hold on your card has been fully released.
                            You have not been charged anything, and no payment was processed.
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- What happened -->
          <tr>
            <td style="padding:32px 40px 24px;">
              <p style="margin:0 0 12px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">What Happened</p>
              <p style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:15px; color:{BRAND_DARK}; line-height:1.6;">
                {reason_text}
              </p>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:15px; color:{BRAND_DARK}; line-height:1.6;">
                This can happen with last-minute deals — popular slots move fast and sometimes another
                customer gets there first. We know that's frustrating, and we're sorry.
              </p>
            </td>
          </tr>

          <!-- Booking details they tried -->
          <tr>
            <td style="padding:0 40px 8px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
                     style="background-color:{BRAND_BG}; border-radius:10px; padding:20px 24px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                               color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">Attempted Booking</p>
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      {_detail_row("Service", service)}
                      {_detail_row("Location", location)}
                      {_detail_row("Date &amp; Time", date_str)}
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- Try again CTA -->
          <tr>
            <td style="padding:32px 40px 40px; text-align:center;">
              <p style="margin:0 0 8px; font-family:{FONT_STACK}; font-size:16px; font-weight:700; color:{BRAND_DARK};">
                Want to find another deal?
              </p>
              <p style="margin:0 0 24px; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_GRAY};">
                New slots are added all the time. Check back and grab the next one.
              </p>
              {_cta_button("Browse Available Deals", retry_url, BRAND_NAVY)}
              <p style="margin:24px 0 0; font-family:{FONT_STACK}; font-size:13px; color:{BRAND_GRAY};">
                Or reply to this email and we'll do our best to find you a great alternative.
              </p>
            </td>
          </tr>
    """

    plain = textwrap.dedent(f"""
        BOOKING UPDATE — LastMinuteDeals
        =================================

        Hi {first_name},

        We're sorry — we weren't able to complete your booking for {service}.

        !! YOUR CARD WAS NOT CHARGED !!
        Any temporary hold on your card has been fully released.
        You have not been charged anything.

        WHAT HAPPENED
        -------------
        {reason_text}

        This can happen with last-minute deals — popular slots move fast.
        We're sorry for the inconvenience.

        ATTEMPTED BOOKING
        -----------------
        Service:     {service}
        Location:    {location}
        Date & Time: {date_str}

        TRY AGAIN
        ---------
        New slots are added all the time. Browse available deals:
        {retry_url}

        Or reply to this email — we'll help find you an alternative.
        — LastMinuteDeals
    """).strip()

    return html_body, plain


def _build_cancelled_html(customer_name: str, slot: dict, confirmation_number: str,
                          refund_status: str = "",
                          cancelled_by_customer: bool = False) -> tuple[str, str]:
    """Email sent when a booking is cancelled (operator, agent, or customer self-serve)."""
    service    = slot.get("service_name", "Your Experience")
    city       = slot.get("location_city", "")
    state      = slot.get("location_state", "")
    location   = f"{city}, {state}".strip(", ") or "—"
    date_str   = _format_dt(slot.get("start_time", ""))
    price      = _format_price(slot.get("our_price") or slot.get("price"), slot.get("currency", "USD"))
    first_name = customer_name.split()[0] if customer_name else "there"
    brand_name = os.environ.get("EMAIL_FROM_NAME", "LastMinuteDeals")
    site_url   = "https://lastminutedealshq.com"

    refund_note = refund_status or "A full refund has been issued to your original payment method."

    # Hero body copy differs depending on who initiated the cancellation.
    # cancelled_by_customer=True: customer clicked the cancel link themselves.
    # False (default): operator/supplier cancelled, or agent-initiated DELETE /bookings.
    if cancelled_by_customer:
        hero_body = (
            f"Hi {first_name}, we've processed your cancellation for "
            f"<strong style=\"color:{BRAND_DARK};\">{service}</strong>. "
            f"We've taken care of the refund for you."
        )
        plain_hero = f"We've processed your cancellation for {service}."
    else:
        hero_body = (
            f"Hi {first_name}, we're sorry — the operator has cancelled your booking for "
            f"<strong style=\"color:{BRAND_DARK};\">{service}</strong>. "
            f"We know this is disappointing, and we've taken care of the refund for you."
        )
        plain_hero = f"We're sorry to let you know that the operator has cancelled your booking:"

    html_body = f"""
          <!-- Hero -->
          <tr>
            <td class="pad-mobile" style="padding:40px 40px 32px;">
              <p style="margin:0 0 6px; font-family:{FONT_STACK}; font-size:13px; font-weight:700;
                         color:{BRAND_ERROR}; text-transform:uppercase; letter-spacing:1px;">Booking Cancelled</p>
              <h1 style="margin:0 0 16px; font-family:{FONT_STACK}; font-size:28px; font-weight:800;
                          color:{BRAND_NAVY}; line-height:1.2; letter-spacing:-0.5px;">
                Your booking has been cancelled
              </h1>
              <p style="margin:0; font-family:{FONT_STACK}; font-size:16px; color:{BRAND_GRAY}; line-height:1.6;">
                {hero_body}
              </p>
            </td>
          </tr>

          <!-- Refund badge -->
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="background-color:#ecfdf5; border:1.5px solid #6ee7b7; border-radius:10px; padding:18px 24px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        <td style="width:32px; vertical-align:top; padding-right:14px;">
                          <div style="width:32px; height:32px; background-color:{BRAND_SUCCESS}; border-radius:50%;
                                      text-align:center; line-height:34px; font-size:18px;">&#x1F4B0;</div>
                        </td>
                        <td>
                          <p style="margin:0 0 4px; font-family:{FONT_STACK}; font-size:15px; font-weight:800; color:#065f46;">
                            Full refund issued
                          </p>
                          <p style="margin:0; font-family:{FONT_STACK}; font-size:13px; color:#064e3b; line-height:1.5;">
                            {refund_note} Refunds typically appear within 3–5 business days depending on your bank.
                          </p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- Cancelled booking details -->
          <tr>
            <td style="padding:32px 40px 8px;">
              <p style="margin:0 0 12px; font-family:{FONT_STACK}; font-size:11px; font-weight:700;
                         color:{BRAND_GRAY}; text-transform:uppercase; letter-spacing:1px;">Cancelled Booking</p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 40px 32px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
                     style="background-color:{BRAND_BG}; border-radius:10px; padding:20px 24px;">
                <tr><td>
                  {_detail_row("Experience", service)}
                  {_detail_row("Date &amp; Time", date_str or "—")}
                  {_detail_row("Location", location)}
                  {_detail_row("Confirmation", confirmation_number or "—")}
                  {_detail_row("Amount Refunded", price)}
                </td></tr>
              </table>
            </td>
          </tr>

          {_divider()}

          <!-- Browse more -->
          <tr>
            <td style="padding:32px 40px 40px; text-align:center;">
              <p style="margin:0 0 8px; font-family:{FONT_STACK}; font-size:16px; font-weight:700; color:{BRAND_DARK};">
                Find your next experience
              </p>
              <p style="margin:0 0 24px; font-family:{FONT_STACK}; font-size:14px; color:{BRAND_GRAY}; line-height:1.6;">
                We have new last-minute slots dropping every few hours. See what's available now.
              </p>
              {_cta_button("Browse Last-Minute Deals", site_url)}
            </td>
          </tr>
    """

    plain = textwrap.dedent(f"""
        BOOKING CANCELLED — {brand_name}
        =================================

        Hi {first_name},

        {plain_hero}

          Experience : {service}
          Date       : {date_str or "—"}
          Location   : {location}
          Confirmation: {confirmation_number or "—"}
          Refunded   : {price}

        REFUND: {refund_note}
        Refunds typically appear within 3–5 business days depending on your bank.

        Browse new last-minute deals: {site_url}

        Questions? Reply to this email — we're here to help.

        {brand_name}
        {site_url}
    """).strip()

    return html_body, plain


# ══════════════════════════════════════════════════════════════════════════════
# Subject line builders
# ══════════════════════════════════════════════════════════════════════════════

def _build_subject(email_type: str, slot: dict, confirmation_number: str = "") -> str:
    service  = slot.get("service_name", "Your Booking")
    date_str = ""
    try:
        iso = slot.get("start_time", "")
        if iso:
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            date_str = dt.strftime("%b %-d")
    except Exception:
        try:
            date_str = dt.strftime("%b %d").replace(" 0", " ")
        except Exception:
            date_str = ""

    if email_type == "checkout_created":
        if date_str:
            return f"Complete your booking \u2014 {service} on {date_str}"
        return f"Complete your booking \u2014 {service}"
    elif email_type == "booking_initiated":
        return f"We're confirming your booking \u2014 {service}"
    elif email_type == "booking_confirmed":
        if date_str:
            return f"You're confirmed! {service} \u2014 {date_str}"
        return f"You're confirmed! {service}"
    elif email_type == "booking_failed":
        return f"Update on your booking \u2014 {service}"
    elif email_type == "booking_cancelled":
        return f"Your booking has been cancelled \u2014 full refund issued"
    else:
        return f"Booking update \u2014 {service}"


# ══════════════════════════════════════════════════════════════════════════════
# Transport: SMTP
# ══════════════════════════════════════════════════════════════════════════════

def _send_via_smtp(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str,
) -> bool:
    """Send email via SMTP. Returns True on success."""
    from_addr = os.environ.get("EMAIL_FROM", "")
    from_name = os.environ.get("EMAIL_FROM_NAME", "LastMinuteDeals")
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    use_tls   = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

    if not smtp_host:
        raise ValueError("SMTP_HOST is not set in .env")
    if not from_addr:
        raise ValueError("EMAIL_FROM is not set in .env")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((from_name, from_addr))
    msg["To"]      = to_address
    msg["X-Mailer"] = "LastMinuteDeals/1.0"

    msg.attach(MIMEText(plain_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content,  "html",  "utf-8"))

    if use_tls:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
    else:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)

    try:
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, [to_address], msg.as_bytes())
    finally:
        server.quit()

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Transport: SendGrid HTTP fallback
# ══════════════════════════════════════════════════════════════════════════════

def _send_via_sendgrid(
    to_address: str,
    subject: str,
    html_content: str,
    plain_content: str,
) -> bool:
    """Send email via SendGrid Web API (no library dependency). Returns True on success."""
    import urllib.request

    api_key  = os.environ.get("SENDGRID_API_KEY", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    from_name = os.environ.get("EMAIL_FROM_NAME", "LastMinuteDeals")

    if not api_key:
        raise ValueError("SENDGRID_API_KEY is not set")
    if not from_addr:
        raise ValueError("EMAIL_FROM is not set in .env")

    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_address}]}],
        "from": {"email": from_addr, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain_content},
            {"type": "text/html",  "value": html_content},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        if status not in (200, 202):
            body = resp.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SendGrid returned HTTP {status}: {body}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def send_booking_email(
    email_type: str,
    customer_email: str,
    customer_name: str,
    slot: dict,
    confirmation_number: str = "",
    error_reason: str = "",
    refund_status: str = "",
    cancel_url: str = "",
    cancelled_by_customer: bool = False,
) -> bool:
    """
    Send a transactional booking email.

    Args:
        email_type:          "booking_initiated", "booking_confirmed", "booking_failed",
                             or "booking_cancelled"
        customer_email:      Recipient email address
        customer_name:       Recipient display name (e.g. "Jane Smith")
        slot:                Normalized slot dict (see normalize_slot.py for schema)
        confirmation_number: Booking confirmation ID (used for booking_confirmed/cancelled)
        error_reason:        Human-readable failure reason (used for booking_failed)
        refund_status:       Refund description (used for booking_cancelled)
        cancel_url:          Self-serve cancellation link (used for booking_confirmed)

    Returns:
        True if the email was sent successfully, False otherwise.

    Raises:
        ValueError: For unsupported email_type
    """
    valid_types = {"checkout_created", "booking_initiated", "booking_confirmed", "booking_failed", "booking_cancelled"}
    if email_type not in valid_types:
        raise ValueError(f"email_type must be one of {valid_types}, got: {email_type!r}")

    if not customer_email:
        raise ValueError("customer_email cannot be empty")

    # Build content
    if email_type == "checkout_created":
        body_rows, plain = _build_checkout_created_html(customer_name, slot)
        preheader = (
            f"Your booking for {slot.get('service_name', 'your experience')} is reserved — "
            "click to complete payment and lock it in."
        )
    elif email_type == "booking_initiated":
        body_rows, plain = _build_initiated_html(customer_name, slot)
        preheader = f"We're securing your spot at {slot.get('service_name', 'your booking')} — your card is reserved, not charged."
    elif email_type == "booking_confirmed":
        body_rows, plain = _build_confirmed_html(customer_name, slot, confirmation_number, cancel_url)
        preheader = f"Your booking is confirmed! Show this email when you arrive at {slot.get('service_name', 'your experience')}."
    elif email_type == "booking_cancelled":
        body_rows, plain = _build_cancelled_html(customer_name, slot, confirmation_number, refund_status,
                                                cancelled_by_customer=cancelled_by_customer)
        preheader = f"Your booking for {slot.get('service_name', 'your experience')} has been cancelled. Full refund issued."
    else:  # booking_failed
        body_rows, plain = _build_failed_html(customer_name, slot, error_reason)
        preheader = f"Your card was NOT charged. We couldn't complete your booking for {slot.get('service_name', 'your service')}."

    subject   = _build_subject(email_type, slot, confirmation_number)
    html_full = _html_shell(preheader, body_rows)

    # Attempt SMTP first, fall back to SendGrid
    smtp_configured = bool(os.environ.get("SMTP_HOST"))
    sg_configured   = bool(os.environ.get("SENDGRID_API_KEY"))

    last_error: Optional[Exception] = None

    if smtp_configured:
        try:
            return _send_via_smtp(customer_email, subject, html_full, plain)
        except Exception as exc:
            last_error = exc
            print(f"[send_booking_email] SMTP failed: {exc}", file=sys.stderr)
            if sg_configured:
                print("[send_booking_email] Falling back to SendGrid...", file=sys.stderr)

    if sg_configured:
        try:
            return _send_via_sendgrid(customer_email, subject, html_full, plain)
        except Exception as exc:
            last_error = exc
            print(f"[send_booking_email] SendGrid failed: {exc}", file=sys.stderr)

    if last_error:
        print(
            f"[send_booking_email] All transports failed for {email_type} → {customer_email}. "
            f"Last error: {last_error}",
            file=sys.stderr,
        )
    else:
        print(
            "[send_booking_email] No email transport configured. "
            "Set SMTP_HOST or SENDGRID_API_KEY in .env.",
            file=sys.stderr,
        )

    return False


# ══════════════════════════════════════════════════════════════════════════════
# Test helper
# ══════════════════════════════════════════════════════════════════════════════

_SAMPLE_SLOT = {
    "service_name":    "Hot Stone Massage (60 min)",
    "business_name":   "Serenity Spa & Wellness",
    "business_id":     "test-001",
    "platform":        "mindbody",
    "category":        "wellness",
    "location_city":   "Austin",
    "location_state":  "TX",
    "location_country": "US",
    "start_time":      "2026-03-29T19:00:00Z",
    "end_time":        "2026-03-29T20:00:00Z",
    "duration_minutes": 60,
    "our_price":       49.00,
    "price":           49.00,
    "currency":        "USD",
    "booking_url":     "https://lastminutedealshq.com/deals/hot-stone-massage",
    "data_source":     "api",
    "confidence":      "high",
}


def test_email(to_address: str, email_type: str = "booking_confirmed") -> bool:
    """
    Send a test booking email to the given address.

    Useful for verifying SMTP credentials and HTML rendering.

    Args:
        to_address: Destination email address
        email_type: Which template to test (default: "booking_confirmed")

    Returns:
        True on success, False on failure
    """
    print(f"[test_email] Sending {email_type!r} test email to {to_address!r}...")
    ok = send_booking_email(
        email_type=email_type,
        customer_email=to_address,
        customer_name="Alex Johnson",
        slot=_SAMPLE_SLOT,
        confirmation_number="LMD-8472",
        error_reason="The slot was claimed by another customer moments before we could confirm it.",
    )
    if ok:
        print(f"[test_email] Sent successfully.")
    else:
        print(f"[test_email] Failed. Check SMTP/SendGrid configuration in .env.")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a test LastMinuteDeals booking email.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python tools/send_booking_email.py --test-to you@example.com
              python tools/send_booking_email.py --test-to you@example.com --type booking_initiated
              python tools/send_booking_email.py --test-to you@example.com --type booking_failed
        """),
    )
    parser.add_argument(
        "--test-to",
        metavar="EMAIL",
        required=True,
        help="Recipient email address for the test send",
    )
    parser.add_argument(
        "--type",
        metavar="TYPE",
        default="booking_confirmed",
        choices=["checkout_created", "booking_initiated", "booking_confirmed", "booking_failed"],
        help="Email type to test (default: booking_confirmed)",
    )
    args = parser.parse_args()

    success = test_email(to_address=args.test_to, email_type=args.type)
    sys.exit(0 if success else 1)
