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
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional

import requests

from db import was_alert_sent_recently, record_alert_sent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def send_alert(
    course_id: int,
    course_name: str,
    tee_date: str,       # YYYY-MM-DD
    tee_time: str,       # HH:MM
    price: float,
    holes: Optional[int],
    players_available: Optional[int],
    alert_type: str,     # 'threshold' or 'below_average'
    reason: str,         # human-readable reason string
    config: dict,
) -> bool:
    """
    Fire alerts for a deal tee time.  Returns True if at least one
    notification was sent, False if suppressed by de-duplication.
    """
    dedupe_hours = config.get("dedupe_hours", 6)

    if was_alert_sent_recently(course_id, tee_date, tee_time, hours=dedupe_hours):
        logger.debug(
            "[alerts] Suppressed duplicate for %s %s %s (within %dh window)",
            course_name, tee_date, tee_time, dedupe_hours,
        )
        return False

    message = _format_message(
        course_name, tee_date, tee_time, price, holes, players_available, reason
    )

    sent = False

    # --- Discord ---
    webhook_url = _get_webhook_url(config)
    if webhook_url:
        sent = _send_discord(webhook_url, message) or sent

    # --- Email ---
    smtp_cfg = config.get("smtp", {})
    if smtp_cfg.get("enabled", False):
        sent = _send_email(smtp_cfg, message, course_name, tee_date, tee_time) or sent

    if sent:
        record_alert_sent(course_id, tee_date, tee_time, price, alert_type)
        logger.info(
            "[alerts] Sent %s alert: %s %s %s $%.2f",
            alert_type, course_name, tee_date, tee_time, price,
        )

    return sent


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_message(
    course_name: str,
    tee_date: str,
    tee_time: str,
    price: float,
    holes: Optional[int],
    players_available: Optional[int],
    reason: str,
) -> str:
    """Build a compact, human-readable alert string."""
    # Parse date for a friendlier display (e.g. "Mon Jun 23")
    try:
        dt = datetime.strptime(tee_date, "%Y-%m-%d")
        date_str = dt.strftime("%a %b %-d")   # e.g. "Mon Jun 23"
    except ValueError:
        date_str = tee_date

    # Convert HH:MM 24-hour → 12-hour with AM/PM
    try:
        t = datetime.strptime(tee_time, "%H:%M")
        time_str = t.strftime("%-I:%M %p")    # e.g. "8:00 AM"
    except ValueError:
        time_str = tee_time

    holes_str = f"{holes}-hole" if holes else "?"
    players_str = f"{players_available} open" if players_available else "?"

    return (
        f"⛳ **Deal Alert — {course_name}**\n"
        f"📅 {date_str}  🕐 {time_str}  ({holes_str})\n"
        f"💰 **${price:.2f}**  |  👤 Spots: {players_str}\n"
        f"📊 {reason}\n"
        f"🔗 https://city-of-arlington.book.teeitup.com"
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _get_webhook_url(config: dict) -> Optional[str]:
    """
    Prefer env var DISCORD_WEBHOOK (for GitHub Actions secrets),
    fall back to config file value.
    """
    import os
    url = os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook_url", "")
    if url and url != "YOUR_DISCORD_WEBHOOK_URL_HERE":
        return url
    return None


def _send_discord(webhook_url: str, message: str) -> bool:
    """POST to Discord webhook.  Returns True on success."""
    try:
        resp = requests.post(
            webhook_url,
            json={"content": message},
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
    # Strip Discord markdown for plain-text email
    plain = (
        message.replace("**", "")
               .replace("⛳", "")
               .replace("📅", "Date:")
               .replace("🕐", "")
               .replace("💰", "Price:")
               .replace("👤", "")
               .replace("📊", "Reason:")
               .replace("🔗", "Booking:")
    )

    msg = MIMEText(plain, "plain")
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
    import sys, json, os

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
        course_name="Tierra Verde (TEST)",
        tee_date="2025-01-01",
        tee_time="14:00",
        price=29.99,
        holes=18,
        players_available=4,
        alert_type="threshold",
        reason="$29.99 is below the $40 weekday threshold",
        config=cfg,
    )
    print("Alert sent:", ok)
