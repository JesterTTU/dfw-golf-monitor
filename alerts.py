"""
alerts.py — Notification dispatch for the tee-time monitor.

Supports:
  • Discord webhook  (always available if webhook URL is set)
  • SMTP email       (optional; enabled via config smtp.enabled = true)

De-duplication:
  Before sending, we check alerts_sent via db.was_alert_sent_recently().
  If an identical slot was alerted in the last N hours we skip it silently.
"""

import smtplib
import json
import logging
import os
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional

import requests

from db import get_last_alerted_price, record_alert_sent

logger = logging.getLogger(__name__)

# Booking URL — takes the user to the course's TeeItUp booking page.
# {alias} is the x-be-alias from config.json (e.g. "tangle-ridge-golf-club").
BOOKING_URL = "https://{alias}.book.teeitup.golf"

# Discord embed colors (decimal)
COLORS = {
    "hot_deal":      5763719,   # green
    "threshold":     3447003,   # blue
    "below_average": 10181046,  # purple
}


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def send_alert(
    course_id: int,
    course_name: str,
    tee_date: str,       # YYYY-MM-DD
    tee_time: str,       # HH:MM local
    price: float,
    holes: Optional[int],
    players_available: Optional[int],
    alert_type: str,     # 'hot_deal', 'threshold', or 'below_average'
    reason: str,         # human-readable reason string
    config: dict,
    booking_alias: str = "city-of-arlington",   # TeeItUp x-be-alias for this course
) -> bool:
    """
    Fire alerts for a deal tee time.  Returns True if at least one
    notification was sent, False if suppressed by de-duplication.
    """
    dedupe_hours = config.get("dedupe_hours", 6)

    last_price = get_last_alerted_price(course_id, tee_date, tee_time, hours=dedupe_hours)
    if last_price is not None:
        if abs(last_price - price) < 0.01:
            # Same price — suppress to avoid noise
            logger.debug(
                "[alerts] Suppressed duplicate for %s %s %s — price unchanged at $%.2f",
                course_name, tee_date, tee_time, price,
            )
            return False
        else:
            # Price changed — allow the alert and note the change
            delta = price - last_price
            direction = "dropped" if delta < 0 else "increased"
            reason = f"{reason}  |  💸 Price {direction} ${abs(delta):.2f} (was ${last_price:.2f})"
            logger.debug(
                "[alerts] Price changed for %s %s %s: $%.2f → $%.2f",
                course_name, tee_date, tee_time, last_price, price,
            )

    booking_url = BOOKING_URL.format(alias=booking_alias)

    embed = _build_embed(
        course_name=course_name,
        tee_date=tee_date,
        tee_time=tee_time,
        price=price,
        holes=holes,
        players_available=players_available,
        alert_type=alert_type,
        reason=reason,
        booking_url=booking_url,
    )

    sent = False

    # --- Discord ---
    # Hot deals → dedicated channel; everything else → original channel
    if alert_type == "hot_deal":
        webhook_url = _get_hot_deals_webhook_url(config)
    else:
        webhook_url = _get_webhook_url(config)
    if webhook_url:
        sent = _send_discord(webhook_url, embed) or sent

    # --- Email ---
    smtp_cfg = config.get("smtp", {})
    if smtp_cfg.get("enabled", False):
        plain_text = _format_plain_text(
            course_name, tee_date, tee_time, price, holes,
            players_available, reason, booking_url
        )
        sent = _send_email(smtp_cfg, plain_text, course_name, tee_date, tee_time) or sent

    if sent:
        record_alert_sent(course_id, tee_date, tee_time, price, alert_type)
        logger.info(
            "[alerts] Sent %s alert: %s %s %s $%.2f",
            alert_type, course_name, tee_date, tee_time, price,
        )

    return sent


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------

def _build_embed(
    course_name: str,
    tee_date: str,
    tee_time: str,
    price: float,
    holes: Optional[int],
    players_available: Optional[int],
    alert_type: str,
    reason: str,
    booking_url: str,
) -> dict:
    """
    Build a Discord embed dict.  The embed title links directly to the
    booking page for that course and date.
    """
    # Friendly date: "Saturday, Jun 28"
    try:
        dt = datetime.strptime(tee_date, "%Y-%m-%d")
        date_str = dt.strftime("%A, %b %-d")
    except ValueError:
        date_str = tee_date

    # 24h → 12h
    try:
        t = datetime.strptime(tee_time, "%H:%M")
        time_str = t.strftime("%-I:%M %p")
    except ValueError:
        time_str = tee_time

    holes_str   = f"{holes}" if holes else "?"
    players_str = str(players_available) if players_available else "?"

    type_labels = {
        "hot_deal":      "🔥 Hot Deal",
        "threshold":     "💲 Price Alert",
        "below_average": "📉 Below Average",
    }
    type_label = type_labels.get(alert_type, "⛳ Deal")

    embed = {
        "title": f"⛳ {type_label} — {course_name}",
        "url":   booking_url,           # clicking the title opens booking page
        "color": COLORS.get(alert_type, 3447003),
        "fields": [
            {
                "name":   "📅 Date",
                "value":  date_str,
                "inline": True,
            },
            {
                "name":   "🕐 Tee Time",
                "value":  time_str,
                "inline": True,
            },
            {
                "name":   "💰 Price",
                "value":  f"**${price:.2f}**",
                "inline": True,
            },
            {
                "name":   "⛳ Holes",
                "value":  holes_str,
                "inline": True,
            },
            {
                "name":   "👤 Spots Open",
                "value":  players_str,
                "inline": True,
            },
            {
                "name":   "📊 Why",
                "value":  reason,
                "inline": False,
            },
            {
                "name":   "🔗 Book Now",
                "value":  f"[Open booking page]({booking_url})",
                "inline": False,
            },
        ],
        "footer": {
            "text": "DFW Golf Monitor • 16 courses",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed


# ---------------------------------------------------------------------------
# Plain-text fallback (for email)
# ---------------------------------------------------------------------------

def _format_plain_text(
    course_name: str,
    tee_date: str,
    tee_time: str,
    price: float,
    holes: Optional[int],
    players_available: Optional[int],
    reason: str,
    booking_url: str,
) -> str:
    try:
        dt = datetime.strptime(tee_date, "%Y-%m-%d")
        date_str = dt.strftime("%A, %b %-d")
    except ValueError:
        date_str = tee_date

    try:
        t = datetime.strptime(tee_time, "%H:%M")
        time_str = t.strftime("%-I:%M %p")
    except ValueError:
        time_str = tee_time

    holes_str   = f"{holes}-hole" if holes else "?"
    players_str = f"{players_available}" if players_available else "?"

    return (
        f"Deal Alert — {course_name}\n"
        f"Date:    {date_str}\n"
        f"Time:    {time_str}  ({holes_str})\n"
        f"Price:   ${price:.2f}\n"
        f"Spots:   {players_str} open\n"
        f"Reason:  {reason}\n"
        f"Book at: {booking_url}\n"
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _get_webhook_url(config: dict) -> Optional[str]:
    url = os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook_url", "")
    if url and url not in ("YOUR_DISCORD_WEBHOOK_URL_HERE", "TEST_SKIP", ""):
        return url
    return None


def _get_hot_deals_webhook_url(config: dict) -> Optional[str]:
    """Returns the dedicated Hot Deals channel webhook, falling back to the main webhook."""
    url = os.environ.get("DISCORD_WEBHOOK_HOT_DEALS") or config.get("discord_webhook_hot_deals_url", "")
    if url and url not in ("YOUR_DISCORD_WEBHOOK_URL_HERE", "TEST_SKIP", ""):
        return url
    # Fallback: if no hot-deals webhook configured, use the main one
    return _get_webhook_url(config)


def _send_discord(webhook_url: str, embed: dict) -> bool:
    """POST embed to Discord webhook.  Returns True on success."""
    try:
        resp = requests.post(
            webhook_url,
            json={
                "username": "Golf Monitor",
                "embeds": [embed],
            },
            timeout=10,
        )
        if resp.status_code in (200, 204):
            logger.debug("[alerts] Discord OK (HTTP %d)", resp.status_code)
            return True
        else:
            logger.warning(
                "[alerts] Discord returned HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False
    except requests.RequestException as exc:
        logger.error("[alerts] Discord request failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------

def _send_email(
    smtp_cfg: dict,
    message: str,
    course_name: str,
    tee_date: str,
    tee_time: str,
) -> bool:
    """Send an SMTP email alert.  Returns True on success."""
    host     = smtp_cfg.get("host", "")
    port     = int(smtp_cfg.get("port", 587))
    user     = smtp_cfg.get("user", "")
    password = smtp_cfg.get("password", "")
    to_addr  = smtp_cfg.get("to", "")

    if not all([host, user, password, to_addr]):
        logger.warning("[alerts] SMTP enabled but config is incomplete — skipping email.")
        return False

    subject = f"⛳ Tee Time Deal: {course_name} on {tee_date} at {tee_time}"

    msg = MIMEText(message, "plain")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, [to_addr], msg.as_string())
        logger.debug("[alerts] Email sent to %s", to_addr)
        return True
    except Exception as exc:
        logger.error("[alerts] Email failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Test helper — call directly to verify your webhook is wired up
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        print("ERROR: config.json not found.  Copy config.example.json → config.json first.")
        sys.exit(1)

    with open(config_path) as f:
        cfg = json.load(f)

    from db import init_db
    init_db()

    ok = send_alert(
        course_id=1315,
        course_name="Tierra Verde",
        tee_date="2026-06-28",
        tee_time="14:10",
        price=58.99,
        holes=18,
        players_available=4,
        alert_type="hot_deal",
        reason="Marked 'Hot Deal' by TeeItUp: $58.99 for 18 holes",
        config=cfg,
    )
    print("Alert sent:", ok)
